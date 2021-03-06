

def save_checkpoint(checkpoint_path, *, model, optimizer, step, loss,
                    extra_modules=None, symlink_name=None, amp=None):
    import os
    import numpy as np
    import random
    import torch

    rng_state = dict(
        python=random.getstate(),
        numpy=np.random.get_state(),
        torch=torch.get_rng_state()
    )

    if torch.cuda.is_available():
        rng_state['torch_cuda'] = torch.cuda.get_rng_state()

    checkpoint_dict = dict(
        rng_state=rng_state,
        state_dict=model.state_dict(),
        optimizer=optimizer.state_dict(),
        step=step,
        loss=loss
    )

    if extra_modules is not None:
        for key, module in extra_modules:
            checkpoint_dict[f'ext.{key}'] = module.state_dict()

    if amp is not None:
        checkpoint_dict['amp'] = amp.state_dict()

    torch.save(checkpoint_dict, checkpoint_path)

    if symlink_name is not None:
        checkpoint_dir = os.path.dirname(checkpoint_path)
        checkpoint_name = os.path.basename(checkpoint_path)

        symlink_path = os.path.join(checkpoint_dir, symlink_name)
        if os.path.exists(symlink_path):
            os.unlink(symlink_path)

        os.symlink(checkpoint_name, symlink_path)


def load_checkpoint(checkpoint_path, *, model=None, optimizer=None, extra_modules=None, strict=False, amp=None,
                    load_rnd_state=True):
    import os
    import numpy as np
    import random
    import torch

    assert os.path.isfile(checkpoint_path)

    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')

    if load_rnd_state:
        rng_state = checkpoint_dict['rng_state']

        random.setstate(rng_state['python'])
        np.random.set_state(rng_state['numpy'])
        torch.set_rng_state(rng_state['torch'])
        if torch.cuda.is_available() and 'torch_cuda' in rng_state:
            torch.cuda.set_rng_state(rng_state['torch_cuda'])

    if model is not None:
        model.load_state_dict(checkpoint_dict['state_dict'], strict=strict)

    if optimizer is not None:
        optimizer.load_state_dict(checkpoint_dict['optimizer'])

    if extra_modules is not None:
        for key, module in extra_modules:
            state = checkpoint_dict.get(f'ext.{key}')
            if strict and state is None:
                raise KeyError(f'Cannot find `{key}` in the checkpoint')
            module.load_state_dict(state)

    if amp is not None and 'amp' in checkpoint_dict:
        amp.load_state_dict(checkpoint_dict['amp'])

    step = checkpoint_dict['step']
    loss = checkpoint_dict['loss']

    return step, loss


class _TrainStep:
    @property
    def last_result(self):
        return self._last_result

    def __init__(self, step_func):
        self._step_func = step_func

        self._last_result = None

    def __call__(self, *args, **kwargs):
        self._last_result = self._step_func(*args, **kwargs)
        return self._last_result.loss


def default_summary_write(writer, model, optimizer, metrics, step, batch, result):
    from . import summary as summary_utils

    summary_utils.write_gradients(writer, model, step)

    n_groups = len(optimizer.param_groups)
    for i, group in enumerate(optimizer.param_groups):
        for k, v in group.items():
            if n_groups == 1:
                label = f'optimizer/{k}'
            else:
                label = f'optimizer/{i}/{k}'

            try:
                writer.add_scalar(label, v, global_step=step)
            except Exception:
                pass

    for k, v in metrics.items():
        try:
            writer.add_scalar(k, v, global_step=step)
        except Exception:
            pass


def default_calc_metrics(model, batch, result):
    return dict(loss=result.loss.item())


def train(*, epochs, model, optimizer, step_func,
          train_dataset, val_dataset=None,
          training_dir=None, checkpoint_path=None,
          summary_write=None,
          calc_metrics=None, metrics_average=None, metrics_map=None,
          val_calc_metrics=None, val_metrics_average=None, val_metrics_map=None,
          device=None, params_ops=None,
          epochs_per_summary=1, epochs_per_checkpoint=1,
          amp=None, checkpoints_limit=None, checkpoint_extra_modules=None):
    import os
    import time
    import torch
    import re
    import shutil
    from torch.utils.tensorboard import SummaryWriter

    CHECKPOINT_PATTERN = r'^checkpoint_(\d+).pth$'
    checkpoint_regex = re.compile(CHECKPOINT_PATTERN)

    from . import eval as eval_utils
    from . import model as model_utils

    if not isinstance(step_func, model_utils._ForwardStepWrapper):
        step_func = model_utils._ForwardStepWrapper(step_func)

    if device is None:
        device = next(model.parameters()).device

    if calc_metrics is None:
        calc_metrics = default_calc_metrics

    if not isinstance(calc_metrics, model_utils._CalcMetricsWrapper):
        calc_metrics = model_utils._CalcMetricsWrapper(calc_metrics)

    if summary_write is None:
        summary_write = default_summary_write

    if checkpoints_limit is not None:
        checkpoints_limit = max(1, checkpoints_limit)

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    if val_calc_metrics is None:
        val_calc_metrics = calc_metrics

    if metrics_average is None:
        metrics_average = eval_utils.get_metrics_ema()

    if val_metrics_map is None:
        val_metrics_map = metrics_map

    assert isinstance(metrics_average, eval_utils.MetricsAverageAbstract), \
        'Must derive `MetricsAverageAbstract`'

    assert val_metrics_average is None or isinstance(val_metrics_average, eval_utils.MetricsAverageAbstract), \
        'Must derive `MetricsAverageAbstract`'

    if metrics_average is None:
        pass

    model.to(device)

    # load checkpoint

    if checkpoint_path is not None:
        _state = load_checkpoint(checkpoint_path, model=model, optimizer=optimizer,
                                 extra_modules=checkpoint_extra_modules, amp=amp)
        epoch_offset, loss = _state
    else:
        epoch_offset, loss = 0, -1

    if training_dir is None:
        training_dir = './training'

    if not os.path.exists(training_dir):
        os.makedirs(training_dir)

    checkpoint_dir = os.path.join(training_dir, 'ckpts')
    summary_dir = os.path.join(training_dir, 'summary')

    if checkpoint_path is None:  # starting from 0, so we can delete all checkpoints an summary
        if os.path.exists(checkpoint_dir):
            shutil.rmtree(checkpoint_dir)
        if os.path.exists(summary_dir):
            shutil.rmtree(summary_dir)

    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)

    checkpoints_history = [ckpt for ckpt in os.listdir(checkpoint_dir)
                           if os.path.isfile(os.path.join(checkpoint_dir, ckpt))]
    if checkpoints_history:
        checkpoints_history = map(lambda x: (checkpoint_regex.match(x), x), checkpoints_history)
        checkpoints_history = ((int(m.groups()[0]), ckpt) for m, ckpt in checkpoints_history if m)
        checkpoints_history = sorted(checkpoints_history, key=lambda x: x[0])
        checkpoints_history = list(map(lambda x: x[1], checkpoints_history))

    checkpoint_last_name = 'checkpoint_last.pth'
    checkpoint_best_path = os.path.join(checkpoint_dir, 'checkpoint_best.pth')

    # meeting the limit

    if checkpoints_limit is not None:
        while len(checkpoints_history) > checkpoints_limit:
            checkpoint_path = os.path.join(checkpoint_dir, checkpoints_history[0])
            del checkpoints_history[0]
            os.remove(checkpoint_path)

    # creating summary writers

    if not os.path.exists(summary_dir):
        os.makedirs(summary_dir)

    train_writer = SummaryWriter(log_dir=os.path.join(summary_dir, 'train'))
    if val_dataset is None:
        valid_writer = None
    else:
        valid_writer = SummaryWriter(log_dir=os.path.join(summary_dir, 'valid'))

    print('Training started:', type(model).__name__)

    if checkpoint_path is not None:
        print(f'epoch #{epoch_offset}/{epochs}', f'loss: {loss:.3f}', flush=True)

        if os.path.exists(checkpoint_best_path):
            checkpoint_best_data = load_checkpoint(checkpoint_best_path, load_rnd_state=False)
        else:
            checkpoint_best_data = epoch_offset, loss
            shutil.copy2(checkpoint_path, checkpoint_best_path)
    else:
        # TODO: place into checkpoints_limit training loop
        with model_utils.evaluating(model):
            with torch.no_grad():
                # train metrics
                batch = next(iter(train_dataset))
                batch = [item.to(device) for item in batch]

                metrics, result = eval_utils.evaluate_batch(model, batch,
                                                            step_func=step_func,
                                                            calc_metrics=calc_metrics,
                                                            ret_result=True)
                if metrics_map is not None:
                    metrics = metrics_map(metrics, 1)
                summary_write(train_writer, model, optimizer, metrics, epoch_offset, batch, result)
                train_writer.flush()

                loss = metrics['loss']

                # valid metrics
                if valid_writer is not None:
                    batch = next(iter(val_dataset))
                    batch = [item.to(device) for item in batch]

                    metrics, result = eval_utils.evaluate_batch(model, batch,
                                                                step_func=step_func,
                                                                calc_metrics=val_calc_metrics,
                                                                ret_result=True)
                    if val_metrics_map is not None:
                        metrics = val_metrics_map(metrics, 1)
                    summary_write(valid_writer, model, optimizer, metrics, epoch_offset, batch, result)
                    valid_writer.flush()

                print(f'epoch #0/{epochs}', f'loss: {loss:.3f}', flush=True)

                # zero checkpoint
                checkpoints_history.append('checkpoint_0.pth')
                checkpoint_path = os.path.join(checkpoint_dir, checkpoints_history[-1])
                save_checkpoint(checkpoint_path,
                                model=model,
                                optimizer=optimizer,
                                step=0,
                                loss=loss,
                                extra_modules=checkpoint_extra_modules,
                                symlink_name=checkpoint_last_name,
                                amp=amp)

                checkpoint_best_data = 0, loss
                shutil.copy2(checkpoint_path, checkpoint_best_path)

    train_step = _TrainStep(step_func)

    # train loop
    for epoch in range(epoch_offset, epochs):
        running_metrics = dict()

        epoch_step = 1
        epoch_timer_start = time.time()

        model.train()
        metrics_average._do_init()
        for epoch_step, batch in enumerate(train_dataset, 1):
            batch = [item.to(device) for item in batch]
            # zero the parameter gradients
            optimizer.zero_grad()

            loss = train_step(model, batch)

            if amp is not None:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            if params_ops:
                for op in params_ops:
                    parameters = model.parameters() if amp is None else amp.master_params(optimizer)
                    op(parameters)

            optimizer.step()

            metrics = calc_metrics(model, batch, train_step.last_result)

            metrics_average(running_metrics, metrics)
        else:
            epoch += 1  # due to the end of the epoch

            if metrics_map is not None:
                running_metrics = metrics_map(running_metrics, epoch_step)

            loss = running_metrics['loss']

            elapsed = time.time() - epoch_timer_start

            print(f'epoch #{epoch}/{epochs}',
                  f'loss: {loss:.3f}', '--',
                  f'elapsed {elapsed:.3f} sec.', flush=True)

            # train metrics
            if epoch % epochs_per_summary == 0:
                summary_write(train_writer, model, optimizer, running_metrics, epoch, batch, train_step.last_result)
                train_writer.flush()

            if epoch % epochs_per_checkpoint == 0:
                checkpoints_history.append(f'checkpoint_{epoch}.pth')
                if checkpoints_limit is not None and len(checkpoints_history) > checkpoints_limit:
                    checkpoint_path = os.path.join(checkpoint_dir, checkpoints_history[0])
                    del checkpoints_history[0]
                    os.remove(checkpoint_path)

                checkpoint_path = os.path.join(checkpoint_dir, checkpoints_history[-1])
                save_checkpoint(checkpoint_path,
                                model=model,
                                optimizer=optimizer,
                                step=epoch,
                                loss=loss,
                                extra_modules=checkpoint_extra_modules,
                                symlink_name=checkpoint_last_name,
                                amp=amp)

                if valid_writer is None and checkpoint_best_data[1] >= loss:
                    checkpoint_best_data = epoch, loss
                    shutil.copy2(checkpoint_path, checkpoint_best_path)

            if valid_writer is not None and epoch % epochs_per_summary == 0:
                # valid metrics
                metrics, (batch, result) = eval_utils.evaluate(model, val_dataset,
                                                               step_func=step_func,
                                                               calc_metrics=val_calc_metrics,
                                                               metrics_average=val_metrics_average,
                                                               metrics_map=val_metrics_map,
                                                               ret_last_batch=True, device=device)
                summary_write(valid_writer, model, optimizer, metrics, epoch, batch, result)
                valid_writer.flush()

                loss = metrics['loss']

                if checkpoint_best_data[1] >= loss:
                    checkpoint_best_data = epoch, loss
                    save_checkpoint(checkpoint_best_path,
                                    model=model,
                                    optimizer=optimizer,
                                    step=epoch,
                                    loss=loss,
                                    extra_modules=checkpoint_extra_modules,
                                    symlink_name=None,
                                    amp=amp)
    else:
        print('Training finished')
