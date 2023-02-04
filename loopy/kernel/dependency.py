__copyright__ = "Copyright (C) 2023 Addison Alvey-Blanco"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from dataclasses import dataclass
from typing import Optional

import islpy as isl
from islpy import dim_type

import pymbolic.primitives as p

from loopy import LoopKernel
from loopy import InstructionBase
from loopy.symbolic import WalkMapper
from loopy.translation_unit import for_each_kernel


@dataclass(frozen=True)
class HappensAfter:
    variable_name: Optional[str]
    instances_rel: Optional[isl.Map]


class AccessMapMapper(WalkMapper):
    """
    TODO Update this documentation so it reflects proper formatting

    Used instead of BatchedAccessMapMapper to get single access maps for each
    instruction.
    """

    def __init__(self, kernel: LoopKernel, var_names: set):
        self.kernel = kernel
        self._var_names = var_names

        from collections import defaultdict
        self.access_maps = defaultdict(lambda:
                           defaultdict(lambda:
                           defaultdict(lambda: None)))

        super().__init__()

    def map_subscript(self, expr: p.Subscript, inames: frozenset, insn_id: str):

        domain = self.kernel.get_inames_domain(inames)

        WalkMapper.map_subscript(self, expr, inames)

        assert isinstance(expr.aggregate, p.Variable)

        if expr.aggregate.name not in self._var_names:
            return

        arg_name = expr.aggregate.name
        subscript = expr.index_tuple

        from loopy.diagnostic import UnableToDetermineAccessRangeError
        from loopy.symbolic import get_access_map

        try:
            access_map = get_access_map(domain, subscript)
        except UnableToDetermineAccessRangeError:
            return

        if self.access_maps[insn_id][arg_name][inames] is None:
            self.access_maps[insn_id][arg_name][inames] = access_map


@for_each_kernel
def compute_data_dependencies(knl: LoopKernel) -> LoopKernel:
    """
    TODO Update documentation to reflect the proper format

    Relax the lexicographic dependencies generated in
    `add_lexicographic_happens_after` according to data dependencies in the
    program.
    """

    writer_map = knl.writer_map()
    reader_map = knl.reader_map()

    reads = {
        insn.id: insn.read_dependency_names() - insn.within_inames
        for insn in knl.instructions
    }
    writes = {
        insn.id: insn.write_dependency_names() - insn.within_inames
        for insn in knl.instructions
    }

    variables = knl.all_variable_names()
    amap = AccessMapMapper(knl, variables)
    for insn in knl.instructions:
        amap(insn.assignee, insn.within_inames, insn.id)
        amap(insn.expression, insn.within_inames, insn.id)

    def get_relation(insn: InstructionBase, variable: Optional[str]) -> isl.Map:
        return amap.access_maps[insn.id][variable][insn.within_inames]

    def get_unordered_deps(R: isl.Map, S: isl.Map) -> isl.Map:
        # equivalent to the composition R^{-1} o S
        return S.apply_range(R.reverse())

    new_insns = []
    for cur_insn in knl.instructions:

        new_insn = cur_insn.copy()

        # handle read-after-write case
        for var in reads[cur_insn.id]:
            for writer in writer_map.get(var, set()) - {cur_insn.id}:

                # grab writer from knl.instructions
                write_insn = writer
                for insn in knl.instructions:
                    if writer == insn.id:
                        write_insn = insn.copy()
                        break

                # writer is immediately before reader
                if writer in cur_insn.happens_after:
                    read_rel = get_relation(cur_insn, var)
                    write_rel = get_relation(write_insn, var)

                    read_write = get_unordered_deps(read_rel, write_rel)

                    lex_map = cur_insn.happens_after[writer].instances_rel

                    deps = lex_map & read_write

                    new_insn.happens_after.update(
                        {writer: HappensAfter(var, deps)}
                    )

                # TODO
                else:
                    pass

        # handle write-after-read and write-after-write
        for var in writes[cur_insn.id]:

            # TODO write-after-read
            for reader in reader_map.get(var, set()) - {cur_insn.id}:

                read_insn = reader
                for insn in knl.instructions:
                    if reader == insn.id:
                        read_insn = insn.copy()
                        break

                if reader in cur_insn.happens_after:
                    write_rel = get_relation(cur_insn, var)
                    read_rel = get_relation(read_insn, var)

                    write_read = get_unordered_deps(write_rel, read_rel)

                    lex_map = cur_insn.happens_after[reader].instances_rel

                    deps = lex_map & write_read

                    new_insn.happens_after.update(
                        {reader: HappensAfter(var, deps)}
                    )

                # TODO
                else:
                    pass

            # write-after-write
            for writer in writer_map.get(var, set()) - {cur_insn.id}:

                other_writer = writer
                for insn in knl.instructions:
                    if writer == insn.id:
                        other_writer = insn.copy()
                        break

                # other writer is immediately before current writer
                if writer in cur_insn.happens_after:
                    before_write_rel = get_relation(other_writer, var)
                    after_write_rel = get_relation(cur_insn, var)

                    write_write = get_unordered_deps(after_write_rel,
                                                     before_write_rel)

                    lex_map = cur_insn.happens_after[writer].instances_rel

                    deps = lex_map & write_write

                    new_insn.happens_after.update(
                        {writer: HappensAfter(var, deps)}
                    )

                else:
                    pass

        new_insns.append(new_insn)

    return knl.copy(instructions=new_insns)


@for_each_kernel
def add_lexicographic_happens_after(knl: LoopKernel) -> LoopKernel:
    """
    TODO update documentation to follow the proper format

    Determine a coarse "happens-before" relationship between an instruction and
    the instruction immediately preceeding it. This strict execution order is
    relaxed in accordance with the data dependency relations.

    See `loopy.dependency.compute_happens_after` for data dependency generation.
    """

    new_insns = []

    for iafter, insn_after in enumerate(knl.instructions):

        if iafter == 0:
            new_insns.append(insn_after)

        else:

            insn_before = knl.instructions[iafter - 1]
            shared_inames = insn_after.within_inames & insn_before.within_inames

            domain_before = knl.get_inames_domain(insn_before.within_inames)
            domain_after = knl.get_inames_domain(insn_after.within_inames)
            happens_before = isl.Map.from_domain_and_range(
                    domain_before, domain_after
            )

            for idim in range(happens_before.dim(dim_type.out)):
                happens_before = happens_before.set_dim_name(
                        dim_type.out, idim,
                        happens_before.get_dim_name(dim_type.out, idim) + "'"
                )

            n_inames_before = happens_before.dim(dim_type.in_)
            happens_before_set = happens_before.move_dims(
                    dim_type.out, 0,
                    dim_type.in_, 0,
                    n_inames_before).range()

            shared_inames_order_before = [
                    domain_before.get_dim_name(dim_type.out, idim)
                    for idim in range(domain_before.dim(dim_type.out))
                    if domain_before.get_dim_name(dim_type.out, idim)
                    in shared_inames
            ]
            shared_inames_order_after = [
                    domain_after.get_dim_name(dim_type.out, idim)
                    for idim in range(domain_after.dim(dim_type.out))
                    if domain_after.get_dim_name(dim_type.out, idim)
                    in shared_inames
            ]
            assert shared_inames_order_after == shared_inames_order_before
            shared_inames_order = shared_inames_order_after

            affs = isl.affs_from_space(happens_before_set.space)

            lex_set = isl.Set.empty(happens_before_set.space)
            for iinnermost, innermost_iname in enumerate(shared_inames_order):

                innermost_set = affs[innermost_iname].lt_set(
                        affs[innermost_iname+"'"]
                )

                for outer_iname in shared_inames_order[:iinnermost]:
                    innermost_set = innermost_set & (
                            affs[outer_iname].eq_set(affs[outer_iname + "'"])
                    )

                lex_set = lex_set | innermost_set

            lex_map = isl.Map.from_range(lex_set).move_dims(
                    dim_type.in_, 0,
                    dim_type.out, 0,
                    n_inames_before)

            happens_before = happens_before & lex_map

            new_happens_after = {
                insn_before.id: HappensAfter(None, happens_before)
            }

            insn_after = insn_after.copy(happens_after=new_happens_after)

            new_insns.append(insn_after)

    return knl.copy(instructions=new_insns)

# vim: foldmethod=marker
