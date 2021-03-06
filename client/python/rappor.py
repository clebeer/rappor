# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""RAPPOR client library.

Privacy is ensured without a third party by only sending RAPPOR'd data over the
network (as opposed to raw client data).

Note that we use SHA1 for the Bloom filter hash function.
"""
import csv
import hashlib
import random


class Error(Exception):
  pass


class Params(object):
  """RAPPOR encoding parameters.

  These affect privacy/anonymity.  See the paper for details.
  """
  def __init__(self):
    self.num_bloombits = 16      # Number of bloom filter bits (k)
    self.num_hashes = 2          # Number of bloom filter hashes (h)
    self.num_cohorts = 64        # Number of cohorts (m)
    self.prob_p = 0.50           # Probability p
    self.prob_q = 0.75           # Probability q
    self.prob_f = 0.50           # Probability f

    self.flag_oneprr = False     # One PRR for each user/word pair

  # For testing
  def __eq__(self, other):
    return self.__dict__ == other.__dict__

  def __repr__(self):
    return repr(self.__dict__)

  @staticmethod
  def from_csv(f):
    """Read the RAPPOR parameters from a CSV file.

    Args:
      f: file handle

    Returns:
      Params instance.

    Raises:
      rappor.Error: when the file is malformed.
    """
    c = csv.reader(f)
    ok = False
    p = Params()
    for i, row in enumerate(c):

      if i == 0:
        if row != ['k', 'h', 'm', 'p', 'q', 'f']:
          raise Error('Header %s is malformed; expected k,h,m,p,q,f' % row)

      elif i == 1:
        try:
          # NOTE: May raise exceptions
          p.num_bloombits = int(row[0])
          p.num_hashes = int(row[1])
          p.num_cohorts = int(row[2])
          p.prob_p = float(row[3])
          p.prob_p = float(row[4])
          p.prob_q = float(row[5])
        except (ValueError, IndexError) as e:
          raise Error('Row is malformed: %s' % e)
        ok = True

      else:
        raise Error('Params file should only have two rows')

    if not ok:
      raise Error("Expected second row with params")

    return p


class SimpleRandom(object):
  """Returns N 32-bit words where each bit has probability p of being 1."""

  def __init__(self, prob_one, num_bits, rand=None):
    self.prob_one = prob_one
    self.num_bits = num_bits
    self.rand = rand or random.Random()

  def __call__(self):
    p = self.prob_one
    rand_fn = self.rand.random  # cache it for speed

    r = 0
    for i in xrange(self.num_bits):
      bit = rand_fn() < p
      r |= (bit << i)  # using bool as int
    return r


class _RandFuncs(object):
  """Base class for randomness."""

  def __init__(self, params, rand=None):
    """
    Args:
      params: RAPPOR parameters
      rand: optional object satisfying the random.Random() interface.
    """
    self.rand = rand or random.Random()
    self.num_bits = params.num_bloombits
    self.cohort_rand_fn = self.rand.randint


class SimpleRandFuncs(_RandFuncs):

  def __init__(self, params, rand=None):
    _RandFuncs.__init__(self, params, rand=rand)

    self.f_gen = SimpleRandom(params.prob_f, self.num_bits, rand=rand)
    self.p_gen = SimpleRandom(params.prob_p, self.num_bits, rand=rand)
    self.q_gen = SimpleRandom(params.prob_q, self.num_bits, rand=rand)
    self.uniform_gen = SimpleRandom(0.5, self.num_bits, rand=rand)


# Compute masks for rappor's Permanent Randomized Response
# The i^th Bloom Filter bit B_i is set to be B'_i equals
# 1  w/ prob f/2 -- (*) -- f_bits
# 0  w/ prob f/2
# B_i w/ prob 1-f -- (&) -- mask_indices set to 0 here, i.e., no mask
# Output bit indices corresponding to (&) and bits 0/1 corresponding to (*)
def get_rappor_masks(user_id, word, params, rand_funcs):
  """
  Call 3 random functions.  Seed deterministically beforehand if oneprr.
  TODO:
  - Rewrite this to be clearer.  We can use a completely different Random()
    instance in the case of oneprr.
  - Expose it in the simulation.  It doesn't appear to be exercised now.
  """
  if params.flag_oneprr:
    stored_state = rand_funcs.rand.getstate()  # Store state
    rand_funcs.rand.seed(user_id + word)  # Consistently seeded

  assigned_cohort = rand_funcs.cohort_rand_fn(0, params.num_cohorts - 1)
  # Uniform bits for (*)
  f_bits = rand_funcs.uniform_gen()
  # Mask indices are 1 with probability f.
  mask_indices = rand_funcs.f_gen()

  if params.flag_oneprr:                    # Restore state
    rand_funcs.rand.setstate(stored_state)

  return assigned_cohort, f_bits, mask_indices


def get_bf_bit(input_word, cohort, hash_no, num_bloombits):
  """Returns the bit to set in the Bloom filter."""
  h = '%s%s%s' % (cohort, hash_no, input_word)
  sha1 = hashlib.sha1(h).digest()
  # Use last two bytes as the hash.  We to allow want more than 2^8 = 256 bits,
  # but 2^16 = 65536 is more than enough.  Default is 16 bits.
  a, b = sha1[0], sha1[1]
  return (ord(a) + ord(b) * 256) % num_bloombits


class Encoder(object):
  """Obfuscates values for a given user using the RAPPOR privacy algorithm."""

  def __init__(self, params, user_id, rand_funcs=None):
    """
    Args:
      params: RAPPOR Params() controlling privacy
      user_id: user ID, for generating cohort.  (In the simulator, each user
        gets its own Encoder instance.)
      rand_funcs: randomness, can be deterministic for testing.
    """
    self.params = params  # RAPPOR params
    self.user_id = user_id

    self.rand_funcs = rand_funcs or SimpleRandFuncs(params)
    self.p_gen = self.rand_funcs.p_gen
    self.q_gen = self.rand_funcs.q_gen

  def encode(self, word):
    """Compute rappor (Instantaneous Randomized Response)."""
    params = self.params

    cohort, f_bits, mask_indices = get_rappor_masks(self.user_id, word,
                                                    params,
                                                    self.rand_funcs)

    bloom_bits_array = 0
    # Compute Bloom Filter
    for hash_no in xrange(params.num_hashes):
      bit_to_set = get_bf_bit(word, cohort, hash_no, params.num_bloombits)
      bloom_bits_array |= (1 << bit_to_set)

    # Both bit manipulations below use the following fact:
    # To set c = a if m = 0 or b if m = 1
    # c = (a & not m) | (b & m)
    #
    # Compute PRR as
    # f_bits if mask_indices = 1
    # bloom_bits_array if mask_indices = 0

    prr = (f_bits & mask_indices) | (bloom_bits_array & ~mask_indices)
    #print 'prr', bin(prr)

    # Compute instantaneous randomized response:
    # If PRR bit is set, output 1 with probability q
    # If PRR bit is not set, output 1 with probability p
    p_bits = self.p_gen()
    q_bits = self.q_gen()

    #print bin(f_bits), bin(mask_indices), bin(p_bits), bin(q_bits)

    irr = (p_bits & ~prr) | (q_bits & prr)
    #print 'irr', bin(irr)

    return cohort, irr  # irr is the rappor
