import copy
import torch
import numpy as np
import gc
from collections import namedtuple
from functools import reduce
from torch import nn
from base import BaseModel
from beautifultable import BeautifulTable
from pruning.PFEC import DepthwiseSeparableBlock

BLOCKS_LEVEL_SPLIT_CHAR = '.'
DistillationArgs = namedtuple('DistillationArgs', ['old_block_name', 'new_block', 'new_block_name'])


class WrappedStudent(BaseModel):
    def __init__(self, teacher_model, config):
        """
        :param teacher_model: nn.Module object - pretrained model that need to be distilled
        """
        super().__init__()
        self.config = config
        # deep cloning teacher model as we will change it later depends on training purpose
        self.teacher = copy.deepcopy(teacher_model)
        if self.teacher.training:
            self.teacher.eval()
        for param in self.teacher.parameters():
            param.requires_grad = False
        # create student net
        self.model = self.student = copy.deepcopy(self.teacher)

        # pruning info
        self.padding = self.config['pruning']['pruner']['padding']
        self.kernel_size = self.config['pruning']['pruner']['kernel_size']
        self.dilation = self.config['pruning']['pruner']['dilation']

        # distillation args contain the distillation information such as block name, ...
        self.replaced_block_names = []
        # store list of student and teacher block to dump info
        self.student_blocks = list()
        self.teacher_blocks = list()
        # stored output of intermediate layers when
        self.student_hidden_outputs = list()
        self.teacher_hidden_outputs = list()
        # list of handlers for removing later
        self._student_hook_handlers = list()
        self._teacher_hook_handlers = list()

        # auxiliary layer
        self.aux_block_names = list()

    def register_hint_layers(self, block_names):
        """
        Register auxiliary layers for computing hint loss
        :param block_names: str
        :return:
        """
        for block_name in block_names:
            self.aux_block_names.append(block_name)
            # get teacher block to retrieve information such as channel dim,...
            teacher_block = self.get_block(block_name, self.teacher)
            student_block = self.get_block(block_name, self.student)
            # teacher's hook
            teacher_handler = teacher_block.register_forward_hook(lambda m, inp, out: self.teacher_hidden_outputs.append(out))
            self._teacher_hook_handlers.append(teacher_handler)
            # student's hook
            student_handler = student_block.register_forward_hook(lambda m, inp, out: self.student_hidden_outputs.append(out))
            self._student_hook_handlers.append(student_handler)
        gc.collect()
        torch.cuda.empty_cache()

    def unfreeze(self, block_names):
        for block_name in block_names:
            block = self.get_block(block_name, self.student)
            for param in block.parameters():
                param.requires_grad = True

    def replace(self, block_names):
        """
        Replace a block with depthwise conv
        :param block_names: str
        :return:
        """
        for block_name in block_names:
            self.replaced_block_names.append(block_name)
            # get teacher block to retrieve information such as channel dim,...
            teacher_block = self.get_block(block_name, self.teacher)
            self.teacher_blocks.append(teacher_block)
            # replace student block with depth-wise separable block
            replace_block = DepthwiseSeparableBlock(in_channels=teacher_block.in_channels,
                                                    out_channels=teacher_block.out_channels,
                                                    kernel_size=self.kernel_size,
                                                    padding=self.padding,
                                                    dilation=self.dilation,
                                                    groups=teacher_block.in_channels,
                                                    bias=teacher_block.bias)
            self.student_blocks.append(replace_block)
            self._set_block(block_name, replace_block, self.student)

        gc.collect()
        torch.cuda.empty_cache()

    def _remove_hooks(self):
        while self._student_hook_handlers:
            handler = self._student_hook_handlers.pop()
            handler.remove()
        while self._teacher_hook_handlers:
            handler = self._teacher_hook_handlers.pop()
            handler.remove()

    def _set_block(self, block_name, block, model):
        """
        set a hidden block to particular object
        :param block_name: str
        :param block: nn.Module
        :return: None
        """
        block_name_split = block_name.split(BLOCKS_LEVEL_SPLIT_CHAR)
        # suppose the blockname is abc.def.ghk then get module self.teacher.abc.def and set that object's attribute \
        # (in this case 'ghk') to block value
        if len(block_name_split) == 1:
            setattr(self.model, block_name, block)
        else:
            obj = self.get_block(BLOCKS_LEVEL_SPLIT_CHAR.join(block_name_split[:-1]), model)
            attr = block_name_split[-1]
            setattr(obj, attr, block)

    def get_block(self, block_name, model):
        """
        get block from block name
        :param block_name: str - should be st like abc.def.ghk
        :param model: nn.Module - which model that block would be drawn from
        :return: nn.Module - required block
        """
        def _get_block(acc, elem):
            if elem.isdigit():
                layer = acc[int(elem)]
            else:
                layer = getattr(acc, elem)
            return layer

        return reduce(lambda acc, elem: _get_block(acc, elem), block_name.split(BLOCKS_LEVEL_SPLIT_CHAR), model)

    def forward(self, x):
        # flush the output of last forward
        self.student_hidden_outputs = []
        self.teacher_hidden_outputs = []

        # in training mode, the network has to forward 2 times, one for computing teacher's prediction \
        # and another for student's one
        with torch.no_grad():
            teacher_pred = self.teacher(x)

        student_pred = self.student(x)

        return student_pred, teacher_pred

    def inference(self, x):
        self.student_hidden_outputs = []
        self.teacher_hidden_outputs = []
        out = self.student(x)

        return out

    #TODO: Implement
    def reset(self):
        """
        stop pruning current student layers and transfer back to original model
        :return:
        """
        raise Exception('Not Implemented...')
        self._remove_hooks()
        # saving student blocks for later usage
        for blocks in self.student_blocks:
            self.saved_student_blocks.append(blocks)

        self.saved_distillation_args += self.replaced_block_names
        self.student_blocks = nn.ModuleList()
        self.teacher_blocks = nn.ModuleList()
        self.replaced_block_names = []

        # flush memory
        gc.collect()
        torch.cuda.empty_cache()

    def eval(self):
        self.training = False
        self.student.eval()

        return self

    def train(self):
        """
        The parameters of TEACHER's network will always be set to EVAL
        :return: self
        """
        self.training = True
        self.student.train()

        return self

    @staticmethod
    def __get_number_param(mod):
        return sum(p.numel() for p in mod.parameters())

    @staticmethod
    def __dump_module_name(mod):
        ret = ""
        for param in mod.named_parameters():
            ret += str(param[0]) + "\n"
        return ret

    def dump_trainable_params(self):
        model_parameters = filter(lambda p: p.requires_grad, self.parameters())
        params = sum([np.prod(p.size()) for p in model_parameters])
        return '\nTrainable parameters: {}'.format(params)

    def dump_student_teacher_blocks_info(self):
        table = BeautifulTable()
        table.column_headers = ["Block name", "old block",
                                "number params old blk", "new block",
                                "number params new blk"]

        table.left_padding_widths['Block name'] = 1
        table.right_padding_widths['Block name'] = 1
        table.left_padding_widths['old block'] = 1
        table.right_padding_widths['old block'] = 1
        table.left_padding_widths['number params old blk'] = 1
        table.right_padding_widths['number params old blk'] = 1
        table.left_padding_widths['new block'] = 1
        table.right_padding_widths['new block'] = 1
        table.left_padding_widths['number params new blk'] = 1
        table.right_padding_widths['number params new blk'] = 1

        for i in range(len(self.student_blocks)):
            table.append_row([self.replaced_block_names[i],
                              self.__dump_module_name(self.teacher_blocks[i]),
                              str(self.__get_number_param(self.teacher_blocks[i])),
                              self.__dump_module_name(self.student_blocks[i]),
                              str(self.__get_number_param(self.student_blocks[i]))])
        return str(table)

    def __str__(self):
        """
        Model prints with number of trainable parameters
        """
        table = self.dump_student_teacher_blocks_info()
        return super().__str__() + '\n' + table