from abc import ABCMeta, abstractmethod
from collections import OrderedDict

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F

from .. import builder


class BaseRecognizer(nn.Module, metaclass=ABCMeta):
    """Base class for recognizers.

    All recognizers should subclass it.
    All subclass should overwrite:

    - Methods:``forward_train``, supporting to forward when training.
    - Methods:``forward_test``, supporting to forward when testing.

    Args:
        backbone (dict): Backbone modules to extract feature.
        cls_head (dict): Classification head to process feature.
        train_cfg (dict): Config for training. Default: None.
        test_cfg (dict): Config for testing. Default: None.
    """

    def __init__(self, backbone, cls_head, train_cfg=None, test_cfg=None):
        super().__init__()
        self.backbone = builder.build_backbone(backbone)
        self.cls_head = builder.build_head(cls_head)

        self.train_cfg = train_cfg
        self.test_cfg = test_cfg
        self.init_weights()

    def init_weights(self):
        """Initialize the model network weights."""
        self.backbone.init_weights()
        self.cls_head.init_weights()

    def extract_feat(self, imgs):
        """Extract features through a backbone.

        Args:
            imgs (torch.Tensor): The input images.

        Returns:
            torch.tensor: The extracted features.
        """
        x = self.backbone(imgs)
        return x

    def average_clip(self, cls_score):
        """Averaging class score over multiple clips.

        Using different averaging types ('score' or 'prob' or None,
        which defined in test_cfg) to computed the final averaged
        class score.

        Args:
            cls_score (torch.Tensor): Class score to be averaged.

        return:
            torch.Tensor: Averaged class score.
        """
        if 'average_clips' not in self.test_cfg.keys():
            raise KeyError('"average_clips" must defined in test_cfg\'s keys')

        average_clips = self.test_cfg['average_clips']
        if average_clips not in ['score', 'prob', None]:
            raise ValueError(f'{average_clips} is not supported. '
                             f'Currently supported ones are '
                             f'["score", "prob", None]')

        if average_clips == 'prob':
            cls_score = F.softmax(cls_score, dim=1).mean(dim=0, keepdim=True)
        elif average_clips == 'score':
            cls_score = cls_score.mean(dim=0, keepdim=True)
        return cls_score

    @abstractmethod
    def forward_train(self, imgs, labels):
        """Defines the computation performed at every call when training."""
        pass

    @abstractmethod
    def forward_test(self, imgs):
        """Defines the computation performed at every call
        when evaluation and testing."""
        pass

    @staticmethod
    def _parse_losses(losses):
        """Parse the raw outputs (losses) of the network.

        Args:
            losses (dict): Raw output of the network, which usually contain
                losses and other necessary information.

        Returns:
            tuple[Tensor, dict]: (loss, log_vars), loss is the loss tensor
                which may be a weighted sum of all losses, log_vars contains
                all the variables to be sent to the logger.
        """
        log_vars = OrderedDict()
        for loss_name, loss_value in losses.items():
            if isinstance(loss_value, torch.Tensor):
                log_vars[loss_name] = loss_value.mean()
            elif isinstance(loss_value, list):
                log_vars[loss_name] = sum(_loss.mean() for _loss in loss_value)
            else:
                raise TypeError(
                    f'{loss_name} is not a tensor or list of tensors')

        loss = sum(_value for _key, _value in log_vars.items()
                   if 'loss' in _key)

        log_vars['loss'] = loss
        for loss_name, loss_value in log_vars.items():
            # reduce loss when distributed training
            if dist.is_available() and dist.is_initialized():
                loss_value = loss_value.data.clone()
                dist.all_reduce(loss_value.div_(dist.get_world_size()))
            log_vars[loss_name] = loss_value.item()

        return loss, log_vars

    def forward(self, imgs, label=None, return_loss=True):
        """Define the computation performed at every call."""
        if return_loss:
            if label is None:
                raise ValueError('Label should not be None.')
            return self.forward_train(imgs, label)
        else:
            return self.forward_test(imgs)

    def train_step(self, data_batch, optimizer, **kwargs):
        """The iteration step during training.

        This method defines an iteration step during training, except for the
        back propagation and optimizer updating, which are done in an optimizer
        hook. Note that in some complicated cases or models, the whole process
        including back propagation and optimizer updating is also defined in
        this method, such as GAN.

        Args:
            data_batch (dict): The output of dataloader.
            optimizer (:obj:`torch.optim.Optimizer` | dict): The optimizer of
                runner is passed to ``train_step()``. This argument is unused
                and reserved.

        Returns:
            dict: It should contain at least 3 keys: ``loss``, ``log_vars``,
                ``num_samples``.
                ``loss`` is a tensor for back propagation, which can be a
                weighted sum of multiple losses.
                ``log_vars`` contains all the variables to be sent to the
                logger.
                ``num_samples`` indicates the batch size (when the model is
                DDP, it means the batch size on each GPU), which is used for
                averaging the logs.

        """
        imgs = data_batch['imgs']
        label = data_batch['label']

        losses = self.forward(imgs, label)

        loss, log_vars = self._parse_losses(losses)

        outputs = dict(
            loss=loss,
            log_vars=log_vars,
            num_samples=len(next(iter(data_batch.values()))))

        return outputs

    def val_step(self, data_batch, optimizer, **kwargs):
        """The iteration step during validation.

        This method shares the same signature as :func:`train_step`, but used
        during val epochs. Note that the evaluation after training epochs is
        not implemented with this method, but an evaluation hook.
        """
        imgs = data_batch['imgs']

        results = self.forward(imgs, None, return_loss=False)

        outputs = dict(results=results)

        return outputs
