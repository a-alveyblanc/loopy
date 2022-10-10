import islpy as isl

from loopy.symbolic import BatchedAccessMapMapper
from loopy import LoopKernel
from loopy import InstructionBase

from dataclasses import dataclass
from enum import Enum

class DependencyType(Enum):
    """An enumeration of the types of data dependencies found in a program.
    """
    WRITE_READ  = 0
    READ_WRITE  = 1
    WRITE_WRITE = 2

class AccessType(Enum):
    """An enumeration of the types of accesses made by statements in a program.
    """
    READ  = 0
    WRITE = 1

@dataclass(frozen=True)
class HappensBefore: 
    """A class representing a "happens-before" relationship between two
    statements found in a :class:`loopy.LoopKernel`. Used to validate that a
    given kernel transformation respects the data dependencies in a given
    program.

    .. attribute:: happens_before
        The :attr:`id` of a :class:`loopy.InstructionBase` that depends on the
        current :class:`loopy.InstructionBase` instance.

    .. attribute:: variable_name
        The name of the variable in a program that is causing the dependency. 

    .. attribute:: relation
        An :class:`isl.Map` representing the data dependency. The input of the
        map is an iname tuple and the output of the map is a set of iname tuples
        that must execute after the input.

    .. attribute:: dependency_type
        A :class:`DependencyType` of :class:`Enum` representing the dependency
        type (write-read, read-write, write-write). 
    """
    
    happens_before: str
    variable_name: str
    relation: isl.Map
    dependency_type: DependencyType

@dataclass(frozen=True)
class AccessRelation:
    """A class that stores information about a particular array access in a
    program.
    .. attribute:: id
        The instruction id of the statement the access relation is representing.

    .. attribute:: variable_name
        The memory location the access relation is representing.

    .. attribute:: relation
        An :class:`isl.Map` object representing the memory access. The access
        relation is a map from the loop domain to the set of valid array
        indices.

    .. attribute:: access_type
        An :class:`Enum` object representing the type of memory access the
        statement is making. The type of memory access is either a read or a
        write.
    """

    id: str
    variable_name: str
    relation: isl.Map
    access_type: AccessType

def generate_dependency_relations(knl: LoopKernel) -> None:

    bmap: BatchedAccessMapMapper = BatchedAccessMapMapper(knl,
                                                          knl.all_variable_names())
    for insn in knl.instructions:
        bmap(insn.assignee, insn.within_inames)
        bmap(insn.expression, insn.within_inames)

    def get_map(var: str, insn: InstructionBase) -> isl.Map: 
        return bmap.access_maps[var][insn.within_inames]

    def read_var_list(insn: InstructionBase) -> frozenset[str]:
        return insn.read_dependency_names() - insn.within_inames

    def write_var_list(insn: InstructionBase) -> frozenset[str]: 
        return insn.write_dependency_names() - insn.within_inames

    def get_dependency_relation(x: isl.Map, y:isl.Map) -> isl.Map:
        diagonal: isl.Map = isl.Map("{ [i,j] -> [i',j']: i = i' and j = j' }")
        dependency_relation: isl.Map = x.apply_range(y.reverse())
        dependency_relation -= diagonal

        return dependency_relation

    read_maps: list[AccessRelation] = [AccessRelation(insn.id, var, 
                                                      get_map(var, insn),
                                                      AccessType.READ)
                 for insn in knl.instructions
                 for var in read_var_list(insn)]
    write_maps: list[AccessRelation] = [AccessRelation(insn.id, var, 
                                                      get_map(var, insn),
                                                      AccessType.WRITE)
                 for insn in knl.instructions
                 for var in write_var_list(insn)]

    write_read: list[HappensBefore] = [HappensBefore(read.id,
                                                     write.variable_name,
                                                     get_dependency_relation(write.relation,
                                                                             read.relation),
                                                     DependencyType.WRITE_READ)
                                       for write in write_maps
                                       for read in read_maps
                                       if write.variable_name == read.variable_name]
    read_write: list[HappensBefore] = [HappensBefore(write.id,
                                                     read.variable_name,
                                                     get_dependency_relation(read.relation,
                                                                             write.relation),
                                                     DependencyType.READ_WRITE)
                                       for read in read_maps
                                       for write in write_maps
                                       if read.variable_name == write.variable_name]
    write_write: list[HappensBefore] = [HappensBefore(write2.id,
                                                      write1.variable_name,
                                                      get_dependency_relation(write1.relation,
                                                                              write2.relation),
                                                      DependencyType.WRITE_WRITE)
                                        for write1 in write_maps
                                        for write2 in write_maps
                                        if write1.variable_name == write2.variable_name]



