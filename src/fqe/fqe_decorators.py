#   Copyright 2019 Quantum Simulation Technologies Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""Utilities and decorators for converting external types into the fqe
intrinsics
"""

from typing import Dict, Tuple, Union
from functools import wraps

import copy

import numpy

from openfermion import FermionOperator
from openfermion.utils import normal_ordered, is_hermitian

from fqe.hamiltonians import hamiltonian
from fqe.hamiltonians import general_hamiltonian
from fqe.hamiltonians import diagonal_hamiltonian
from fqe.hamiltonians import diagonal_coulomb
from fqe.hamiltonians import gso_hamiltonian
from fqe.hamiltonians import restricted_hamiltonian
from fqe.hamiltonians import sparse_hamiltonian
from fqe.hamiltonians import sso_hamiltonian
from fqe.openfermion_utils import largest_operator_index
from fqe.util import validate_tuple
from fqe.fqe_ops import fqe_ops_utils


def build_hamiltonian(ops: Union[FermionOperator, hamiltonian.Hamiltonian],
                      norb=0,
                      conserve_number=True,
                      e_0=0. + 0.j):
    """Build a Hamiltonian object for the fqe
    """
    if isinstance(ops, hamiltonian.Hamiltonian):
        return ops

    if isinstance(ops, tuple):
        validate_tuple(ops)

        return general_hamiltonian.General(ops, conserve_number=conserve_number, e_0=e_0)

    if not isinstance(ops, FermionOperator):
        raise TypeError('Expected FermionOperator' \
                        ' but received {}.'.format(type(ops)))

    assert is_hermitian(ops)

    if len(ops.terms) <= 2:
        return sparse_hamiltonian.SparseHamiltonian(norb,
                                                    ops,
                                                    conserve_number=conserve_number,
                                                    e_0=e_0)

    if not conserve_number:
        ops = transform_to_spin_broken(ops)

    ops = normal_ordered(ops)

    ops_rank, e_0 = split_openfermion_tensor(ops)

    norb = 0
    for term in ops_rank.values():
        ablk, bblk = largest_operator_index(term)
        norb = max(norb + 1, ablk // 2 + 1, bblk // 2 + 1)

    ops_mat = {}
    for rank, term in ops_rank.items():
        ops_mat[rank] = fermionops_tomatrix(term, norb=norb)


    if len(ops_mat) == 1:
        if 2 in ops_mat:
            return process_rank2_matrix(ops_mat[2],
                                        norb=norb,
                                        conserve_number=conserve_number,
                                        e_0=e_0)

        if 4 in ops_mat:
            if check_diagonal_coulomb(ops_mat[4]):
                return diagonal_coulomb.DiagonalCoulomb(ops_mat[4], e_0=e_0)

    for i in range(2, 9, 2):
        if i not in ops_mat:
            mat_dim = tuple([2*norb for _ in range(i)])
            ops_mat[i] = numpy.zeros(mat_dim)

    return general_hamiltonian.General(tuple([ops_mat[2],
                                              ops_mat[4],
                                              ops_mat[6],
                                              ops_mat[8]]),
                                       conserve_number=conserve_number,
                                       e_0=e_0)


def transform_to_spin_broken(ops):
    """Convert a Fermion Operator string from number broken to spin broken
    operators.
    """
    newstr = FermionOperator()
    for term in ops.terms:
        opstr = ''
        for element in term:
            if element[0] % 2:
                if element[1]:
                    opstr += str(element[0]) + ' '
                else:
                    opstr += str(element[0]) + '^ '
            else:
                if element[1]:
                    opstr += str(element[0]) + '^ '
                else:
                    opstr += str(element[0]) + ' '
        newstr += FermionOperator(opstr, ops.terms[term])
    return newstr


def split_openfermion_tensor(ops: 'FermionOperator'
                            ) -> Tuple[Dict[int, 'FermionOperator'], complex]:
    """Given a string of openfermion operators, split them according to their
    rank.

    Args:
        ops (FermionOperator) - a string of Fermion Operators

    Returns:
        split dict[int] = FermionOperator - a list of Fermion Operators sorted
            according to their degree
    """
    e_0 = 0. + 0.j

    split: Dict[int, 'FermionOperator'] = {}

    for term in ops:
        rank = term.many_body_order()

        if rank % 2:
            raise ValueError('Odd rank term not accepted')

        if rank == 0:
            e_0 += term.terms[()]

        else:
            if rank not in split:
                split[rank] = term
            else:
                split[rank] += term

    return split, e_0


def fermionops_tomatrix(ops, norb):
    """Convert FermionOperators to matrix
    """
    ablk, bblk = largest_operator_index(ops)

    ablk = ablk // 2 + 1
    bblk = (bblk - 1) // 2 + 1

    if norb:
        if norb < ablk:
            raise ValueError('Highest alpha index exceeds the norb of orbitals')
        if norb < bblk:
            raise ValueError('Highest beta index exceeds the norb of orbitals')
        dim = 2*norb

    else:
        dim = max(2*ablk, 2*bblk)

    ablk = dim // 2

    rank = ops.many_body_order()

    if rank % 2:
        raise ValueError('Odd rank operator not supported')

    tensor_dim = [dim for _ in range(rank)]
    index_mask = [0 for _ in range(rank)]

    tensor = numpy.zeros(tensor_dim, dtype=numpy.complex128)

    for term in ops.terms:

        for i in range(rank):

            index = term[i][0]

            if i < rank // 2:
                if not term[i][1]:
                    raise ValueError('Found annihilation operator where' \
                                     'creation is expected')
            elif term[i][1]:
                raise ValueError('Found creattion operator where' \
                                 'annihilation is expected')

            if index % 2:
                ind = (index - 1) // 2 + ablk
            else:
                ind = index // 2

            index_mask[i] = ind

        tensor[tuple(index_mask)] += ops.terms[term]

    return tensor



def fermion_op_to_rank2(ops: 'FermionOperator', norb: int = 0) -> Tuple[numpy.ndarray]:
    """Convert a string of FermionOperators into the super matrix

        | a^ a   a^ a^ |
        | a  a   a  a^ |

    Args:
        ops (FermionOperator) - a string of FermionOperators

    Returns:
        numpy.array(dtype=numpy.complex128)
    """
    ablk, bblk = largest_operator_index(ops)

    ablk = ablk // 2 + 1
    bblk = (bblk - 1) // 2 + 1

    if norb:
        if norb < ablk:
            raise ValueError('Highest alpha index exceeds the norb of orbitals')
        if norb < bblk:
            raise ValueError('Highest beta index exceeds the norb of orbitals')
        dim = 2*norb

    else:
        dim = max(2*ablk, 2*bblk)

    ablk = dim //2

    h1e = numpy.zeros((2*dim, 2*dim), dtype=numpy.complex128)

    if ops.many_body_order() != 2:
        raise ValueError('Rank of operator not-equal 2')

    for term in ops.terms:

        left, right = term[0][0], term[1][0]

        if left % 2:
            ind = (left - 1) // 2 + ablk
        else:
            ind = left // 2

        if right % 2:
            jnd = (right - 1) // 2 + ablk
        else:
            jnd = right // 2

        if term[0][1] and term[1][1]:
            h1e[ind, jnd + dim] += ops.terms[term]

        elif term[0][1] and not term[1][1]:
            h1e[ind, jnd] += ops.terms[term]

        elif not term[0][1] and term[1][1]:
            h1e[ind + dim, jnd + dim] += ops.terms[term]

        else:
            h1e[ind + dim, jnd] += ops.terms[term]

    return h1e


def process_rank2_matrix(mat,
                         norb: int = 0,
                         conserve_number: bool = True,
                         e_0: complex = 0. + 0.j) -> 'hamiltonian.Hamiltonian':
    """Look at the structure of the (1, 0) component of the one body matrix and
    determine the symmetries.
    """
    if not numpy.allclose(mat, mat.conj().T):
        diff = numpy.abs(mat - mat.conj().T)
        index = numpy.unravel_index(diff.argmax(), diff.shape)
        print('Element {} outside tolerance'.format(index))
        print('{} != {} '.format(mat[index], mat[index[1], index[0]].conj()))
        raise ValueError

    if not norb:
        norb = mat.shape[0]

    diagonal = True

    for i in range(1, max(norb, mat.shape[0])):
        for j in range(0, i):
            if mat[i, j] != 0. + 0.j:
                diagonal = False
                break

    if diagonal:
        return diagonal_hamiltonian.Diagonal(mat.diagonal(),
                                             conserve_number=conserve_number,
                                             e_0=e_0)

    if mat[norb:2*norb, :norb].any():
        return gso_hamiltonian.GSOHamiltonian(tuple([mat]),
                                              conserve_number=conserve_number,
                                              e_0=e_0)

    if numpy.allclose(mat[:norb, :norb], mat[norb:, norb:]):
        return restricted_hamiltonian.Restricted(tuple([mat]),
                                                 conserve_number=conserve_number,
                                                 e_0=e_0)

    spin_mat = numpy.zeros((norb, 2*norb), dtype=mat.dtype)
    spin_mat[:, :norb] = mat[:norb, :norb]
    spin_mat[:, norb: 2*norb] = mat[norb:, norb:]
    return sso_hamiltonian.SSOHamiltonian(tuple([spin_mat]),
                                          conserve_number=conserve_number,
                                          e_0=e_0)


def check_diagonal_coulomb(mat) -> bool:
    """Look at the structure of the two body matrix and determine
    if it is diagonal coulomb
    """
    dim = mat.shape[0]

    for i in range(dim):
        for j in range(dim):
            for k in range(dim):
                for l in range(dim):
                    if i == k and j == l:
                        pass
                    elif mat[i, j, k, l] != 0. + 0.j:
                        return False
    return True


def wrap_rdm(rdm):
    """Decorator to convert arguments to the fqe internal classes
    """
    @wraps(rdm)
    def symmetry_process(self, string, brawfn=None):
        if self.conserve_spin() and not self.conserve_number():
            wfn = self._copy_beta_inversion()
        else:
            wfn = copy.deepcopy(self)

        if any(char.isdigit() for char in string):
            if self.conserve_spin() and not self.conserve_number():
                string = fqe_ops_utils.switch_broken_symmetry(string)

        return rdm(wfn, string, brawfn=brawfn)
    return symmetry_process


def wrap_apply(apply):
    """Decorator to convert arguments to the fqe internal classes
    """
    @wraps(apply)
    def convert(self, ops):
        hamil = build_hamiltonian(ops, conserve_number=self.conserve_number())
        return apply(self, hamil)
    return convert


def wrap_time_evolve(time_evolve):
    """Decorator to convert arguments to the fqe internal classes
    """
    @wraps(time_evolve)
    def convert(self, time, ops):
        hamil = build_hamiltonian(ops, conserve_number=self.conserve_number())
        return time_evolve(self, time, hamil)
    return convert


def wrap_apply_generated_unitary(apply_generated_unitary):
    """Decorator to convert arguments to the fqe internal classes
    """
    @wraps(apply_generated_unitary)
    def convert(self, time, algo, ops, accuracy=0.0, expansion=30, spec_lim=None):
        hamil = build_hamiltonian(ops, conserve_number=self.conserve_number())
        return apply_generated_unitary(self,
                                       time,
                                       algo,
                                       hamil,
                                       accuracy=accuracy,
                                       expansion=expansion,
                                       spec_lim=spec_lim)
    return convert
