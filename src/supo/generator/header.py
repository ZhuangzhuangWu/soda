#!/usr/bin/python3.6
import json
import math
import operator
import os
import sys
from fractions import Fraction
from functools import reduce
sys.path.append(os.path.dirname(__file__))
from utils import Stencil, Printer, GetStencilFromJSON

def PrintCode(stencil, header_file):
    p = Printer(header_file)
    p.PrintLine('#ifndef HALIDE_%s_H_' % stencil.app_name.upper())
    p.PrintLine('#define HALIDE_%s_H_' % stencil.app_name.upper())
    p.PrintLine()

    p.PrintLine('#ifndef HALIDE_ATTRIBUTE_ALIGN')
    p.DoIndent()
    p.PrintLine('#ifdef _MSC_VER')
    p.DoIndent()
    p.PrintLine('#define HALIDE_ATTRIBUTE_ALIGN(x) __declspec(align(x))')
    p.UnIndent()
    p.PrintLine('#else')
    p.DoIndent()
    p.PrintLine('#define HALIDE_ATTRIBUTE_ALIGN(x) __attribute__((aligned(x)))')
    p.UnIndent()
    p.PrintLine('#endif')
    p.UnIndent()
    p.PrintLine('#endif//HALIDE_ATTRIBUTE_ALIGN')
    p.PrintLine()

    p.PrintLine('#ifndef BUFFER_T_DEFINED')
    p.PrintLine('#define BUFFER_T_DEFINED')
    p.PrintLine('#include<stdbool.h>')
    p.PrintLine('#include<stdint.h>')
    p.PrintLine('typedef struct buffer_t {')
    p.DoIndent()
    p.PrintLine('uint64_t dev;')
    p.PrintLine('uint8_t* host;')
    p.PrintLine('int32_t extent[4];')
    p.PrintLine('int32_t stride[4];')
    p.PrintLine('int32_t min[4];')
    p.PrintLine('int32_t elem_size;')
    p.PrintLine('HALIDE_ATTRIBUTE_ALIGN(1) bool host_dirty;')
    p.PrintLine('HALIDE_ATTRIBUTE_ALIGN(1) bool dev_dirty;')
    p.PrintLine('HALIDE_ATTRIBUTE_ALIGN(1) uint8_t _padding[10 - sizeof(void *)];')
    p.UnIndent()
    p.PrintLine('} buffer_t;')
    p.PrintLine('#endif//BUFFER_T_DEFINED')
    p.PrintLine()

    p.PrintLine('#ifndef HALIDE_FUNCTION_ATTRS')
    p.PrintLine('#define HALIDE_FUNCTION_ATTRS')
    p.PrintLine('#endif//HALIDE_FUNCTION_ATTRS')
    p.PrintLine()

    buffers = [[stencil.input_name, stencil.input_type], [stencil.output_name, stencil.output_type]]+stencil.extra_params
    p.PrintLine('int %s(%sconst char* xclbin) HALIDE_FUNCTION_ATTRS;' % (stencil.app_name, ''.join([('buffer_t *var_%s_buffer, ') % x[0] for x in buffers])))
    p.PrintLine()

    p.PrintLine('#endif//HALIDE_%s_H_' % stencil.app_name.upper())
    p.PrintLine()

def main():
    stencil = GetStencilFromJSON(sys.stdin)
    PrintCode(stencil, sys.stdout)

if __name__ == '__main__':
    main()