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

# TODO
#   1. Array access discovery needs work
#   2. Can we reduce the length of the code without making things more
#   complicated
#
#   Problem with BatchedAccessMapMapper is that it we cannot distinguish between
#   accesses of particular instructions.
#
#   Problem with AccessMapMapper is that it misses important array access
#   information. Most importantly, it did not detect when an instruction
#   accessed an array multiple times in the same statement
#
#       For example a[i,j] = a[i+1,j+1] - a[i-1,j-1]
#
#   Probably an implementation that is "in the middle" would work for this case

from typing import Mapping

import islpy as isl
from islpy import dim_type

from collections import defaultdict

from loopy import LoopKernel
from loopy.kernel.instruction import HappensAfter
from loopy.symbolic import UncachedWalkMapper as WalkMapper
from loopy.translation_unit import for_each_kernel
from loopy.symbolic import get_access_map

import pymbolic.primitives as p


class AccessMapFinder(WalkMapper):
    """Finds and stores relations representing the accesses of an array by
    statement instances. Access maps can be found using an instruction's ID and
    a variable's name. Essentially a specialized version of
    BatchedAccessMapMapper.
    """

    def __init__(self, knl: LoopKernel) -> None:
        self.kernel = knl
        self.access_maps = defaultdict(lambda: defaultdict(lambda: None))

        super().__init__()

    def get_map(self, insn_id: str, variable_name: str) -> isl.Map:
        try:
            return self.access_maps[insn_id][variable_name]
        except KeyError:
            raise RuntimeError("error while trying to retrieve an access map "
                               "for instruction %s and array %s"
                               % (insn_id, variable_name))

    def map_subscript(self, expr, insn):
        domain = self.kernel.get_inames_domain(insn.within_inames)
        WalkMapper.map_subscript(self, expr, insn)

        assert isinstance(expr.aggregate, p.Variable)

        arg_name = expr.aggregate.name
        subscript = expr.index_tuple

        access_map = get_access_map(
                domain, subscript, self.kernel.assumptions
        )

        if self.access_maps[insn.id][arg_name]:
            self.access_maps[insn.id][arg_name] |= access_map
        else:
            self.access_maps[insn.id][arg_name] = access_map

    def map_linear_subscript(self, expr, insn):
        self.rec(expr.index, insn)

    def map_reduction(self, expr, insn):
        return WalkMapper.map_reduction(self, expr, insn)

    def map_type_cast(self, expr, inames):
        return self.rec(expr.child, inames)

    # TODO implement this
    def map_sub_array_ref(self, expr, insn):
        raise NotImplementedError("functionality for sub-array reference "
                                  "access map finding has not yet been "
                                  "implemented")


@for_each_kernel
def compute_data_dependencies(knl: LoopKernel) -> LoopKernel:
    """Compute data dependencies between statements in a kernel. Can utilize an
    existing lexicographic ordering, i.e. one generated by
    `add_lexicographic_happens_after`.
    """

    def get_unordered_deps(frm: isl.Map, to: isl.Map) -> isl.Map:
        # equivalent to R^{-1} o S
        dependency = frm.apply_range(to.reverse())

        return dependency - dependency.identity(dependency.get_space())

    def make_inames_unique(relation: isl.Map) -> isl.Map:
        """Add a single quote to the output inames of an isl.Map
        """
        for idim in range(relation.dim(dim_type.out)):
            iname = relation.get_dim_name(dim_type.out, idim) + "'"
            relation = relation.set_dim_name(dim_type.out, idim, iname)

        return relation

    writer_map = knl.writer_map()
    reader_map = knl.reader_map()

    # consider all accesses since we union all dependencies anyway
    accesses = {
            insn.id: (insn.read_dependency_names() |
                      insn.write_dependency_names()) - insn.within_inames
            for insn in knl.instructions
    }

    amf = AccessMapFinder(knl)
    for insn in knl.instructions:
        amf(insn.assignee, insn)
        amf(insn.expression, insn)

    new_insns = []
    for before_insn in knl.instructions:
        new_happens_after: Mapping[str, HappensAfter] = {}

        assert isinstance(before_insn.id, str)  # stop complaints

        for variable in accesses[before_insn.id]:
            # get all instruction ids that also access the current variable
            accessed_by = reader_map.get(variable, set()) | \
                          writer_map.get(variable, set())

            # dependency computation
            for after_insn in accessed_by:
                before_map = amf.get_map(before_insn.id, variable)
                after_map = amf.get_map(after_insn, variable)

                unordered_deps = get_unordered_deps(before_map, after_map)

                # may not permanently construct lex maps this way
                lex_map = unordered_deps.lex_lt_map(unordered_deps)

                # may not be needed if we can resolve issues above
                if lex_map.space != unordered_deps.space:
                    lex_map = make_inames_unique(lex_map)
                    unordered_deps = make_inames_unique(unordered_deps)

                    lex_map, unordered_deps = isl.align_two(
                            lex_map, unordered_deps
                    )

                deps = unordered_deps & lex_map

                if not deps.is_empty():
                    new_happens_after.update({
                        after_insn: HappensAfter(variable, deps)
                    })

        new_insns.append(before_insn.copy(happens_after=new_happens_after))

    return knl.copy(instructions=new_insns)


@for_each_kernel
def add_lexicographic_happens_after(knl: LoopKernel) -> LoopKernel:
    """Compute an initial lexicographic happens-after ordering of the statments
    in a :class:`loopy.LoopKernel`. Statements are ordered in a sequential
    (C-like) manner.
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


@for_each_kernel
def print_dependency_info(knl: LoopKernel) -> None:

    dependencies = []
    for insn in knl.instructions:
        dep_string = f"{insn.id} depends on \n"

        if not insn.happens_after:
            dep_string += "nothing"

        else:
            for dep in insn.happens_after:

                dep_string += f"{dep} "

                if insn.happens_after[dep].variable_name is None:
                    dep_string += ""

                else:
                    dep_string += "at variable "
                    dep_string += f"'{insn.happens_after[dep].variable_name}' "

                dep_string += "with relation \n"
                dep_string += f"{insn.happens_after[dep].instances_rel}\n"

        dependencies.append(dep_string)

    for s in dependencies:
        print(s)

# vim: foldmethod=marker
