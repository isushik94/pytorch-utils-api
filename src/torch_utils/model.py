import collections
import numpy as np
import torch
from contextlib import contextmanager


@contextmanager
def evaluating(net):
    '''Temporarily switch to evaluation mode.'''
    istrain = net.training
    try:
        net.eval()
        yield net
    finally:
        if istrain:
            net.train()


class ForwardResult:
    def __init__(self, outputs, targets=None, loss=None):
        self.outputs = outputs
        self.targets = targets
        self.loss = loss


class _CalcMetricsWrapper:
    def __init__(self, func):
        assert callable(func), 'func: must be callable'
        self._func = func

    def __call__(self, *args, **kwargs):
        metrics = self._func(*args, **kwargs)
        return self._normalize(metrics)

    @staticmethod
    def _normalize(metrics):
        def normalize_item(item):
            if isinstance(item, dict):
                return {k: normalize_item(v) for k, v in item.items()}
            elif isinstance(item, list):
                return [normalize_item(v) for v in item]
            elif isinstance(item, np.ndarray):
                return item
            elif isinstance(item, torch.Tensor):
                if len(torch.squeeze(item).shape):
                    return item.detach()
                else:
                    return torch.squeeze(item).item()
            elif isinstance(item, collections.abc.Iterable):
                return tuple(normalize_item(v) for v in item)
            else:
                return item

        return normalize_item(metrics)


class _ForwardStepWrapper:
    def __init__(self, func):
        assert callable(func), 'func: must be callable'
        self._func = func

    def __call__(self, *args, **kwargs):
        r = self._func(*args, **kwargs)

        if isinstance(r, dict):
            return ForwardResult(**r)
        else:
            if not isinstance(r, tuple):
                r = (r,)

            return ForwardResult(*r)


def forward_step(inputs_getter, targets_getter=None, criterion=None,
                 inputs_preprocess=None, outputs_postprocess=None):

    def step_func(model, batch):
        inputs = inputs_getter(batch)

        if inputs_preprocess is not None:
            inputs = inputs_preprocess(inputs)

        if isinstance(inputs, tuple):
            outputs = model(*inputs)
        else:
            outputs = model(inputs)

        if targets_getter is not None:
            targets = targets_getter(batch)
        else:
            targets = None

        if criterion is not None:
            if targets is None:
                if isinstance(inputs, tuple):
                    loss = criterion(*outputs)
                else:
                    loss = criterion(outputs)
            else:
                loss = criterion(outputs, targets)
        else:
            loss = None

        if outputs_postprocess is not None:
            outputs = outputs_postprocess(outputs)

        return ForwardResult(outputs, targets, loss)

    return _ForwardStepWrapper(step_func)
