import argparse
import collections
import torch
import numpy as np
import data_loader as module_data
import losses as module_loss
import models.metric as module_metric
import models as module_arch
from models.student import BaseStudent
from models.deeplabv3 import get_distillation_args
from data_loader import _create_transform
from parse_config import ConfigParser
from trainer.kd_trainer import KnowledgeDistillationTrainer

# fix random seeds for reproducibility
SEED = 123
torch.manual_seed(SEED)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
np.random.seed(SEED)


def main(config):
    logger = config.get_logger('train')

    # setup data_loader instances
    train_joint_transform, train_input_transform, target_transform, val_input_transform = _create_transform(config)
    train_data_loader = config.init_obj('train_data_loader', module_data, transform=train_input_transform,
                                        transforms=train_joint_transform, target_transform=target_transform)
    valid_data_loader = config.init_obj('val_data_loader', module_data, transform=val_input_transform,
                                        target_transform=target_transform)

    # Load pretrained teacher model
    teacher = config.restore_snapshot('teacher', module_arch)
    teacher = teacher.cpu() # saved some memory as student network will use a (deep) copy of teacher model

    # build models architecture, then print to console
    args = get_distillation_args()
    student = BaseStudent(teacher, args)
    logger.info(student)

    # get function handles of loss and metrics
    supervised_criterion = config.init_obj('supervised_loss', module_loss)
    div_criterion = config.init_obj('div_loss', module_loss)
    kd_criterion = config.init_obj('kd_loss', module_loss)
    criterions = [supervised_criterion, div_criterion, kd_criterion]
    metrics = [getattr(module_metric, met) for met in config['metrics']]

    # build optimizer, learning rate scheduler. delete every lines containing lr_scheduler for disabling scheduler
    trainable_params = filter(lambda p: p.requires_grad, student.parameters())
    optimizer = config.init_obj('optimizer', torch.optim, trainable_params)
    lr_scheduler = config.init_obj('lr_scheduler', torch.optim.lr_scheduler, optimizer)

    # Knowledge Distillation only
    if config["KD"]["use"]:
        trainer = KnowledgeDistillationTrainer(student, criterions, metrics, optimizer, config, train_data_loader,
                                               valid_data_loader, lr_scheduler)

    trainer.train()


if __name__ == '__main__':
    args = argparse.ArgumentParser(description='Knowledge Distillation')
    args.add_argument('-c', '--config', default=None, type=str,
                      help='config file path (default: None)')
    args.add_argument('-r', '--resume', default=None, type=str,
                      help='path to latest checkpoint (default: None)')
    args.add_argument('-d', '--device', default=None, type=str,
                      help='indices of GPUs to enable (default: all)')

    # custom cli options to modify configuration from default values given in json file.
    CustomArgs = collections.namedtuple('CustomArgs', 'flags type target')
    options = [
        CustomArgs(['--lr', '--learning_rate'], type=float, target='optimizer;args;lr'),
        CustomArgs(['--bs', '--batch_size'], type=int, target='data_loader;args;batch_size')
    ]
    config = ConfigParser.from_args(args, options)
    main(config)
