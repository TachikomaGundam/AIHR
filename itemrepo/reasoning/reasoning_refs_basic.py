from __future__ import annotations

import math
from fractions import Fraction
from math import comb, gcd

# ---------------------------------------------------------------------------
# Tier 1 warmups
# ---------------------------------------------------------------------------
def ref_t1_sum_1_100():       return sum(range(1, 101))
def ref_t1_gcd():              return gcd(48, 180)
def ref_t1_lcm_set():
    from math import lcm as m_lcm; return m_lcm(8, 12, 18)
def ref_t1_power_sum():        return 2**10 + 3**5
def ref_t1_digit_sum():        return sum(int(d) for d in str(7**6))
def ref_t1_mod():              return pow(17, 5, 7)
def ref_t1_comb():             return comb(10, 2)
def ref_t1_mean():             return sum([12,15,18,21,24]) // 5
def ref_t1_right_area():       return (4 * (5**2 - 4**2)**0.5) // 2
def ref_t1_sq_count():         return sum(1 for i in range(1, 101) if int(i**0.5)**2 == i)

# ---------------------------------------------------------------------------
# Tier 2
# ---------------------------------------------------------------------------
def ref_t2_sum_3digit_div7(): return sum(i for i in range(105, 1000, 7))
def ref_t2_divisor_count_720(): return sum(1 for i in range(1, 721) if 720 % i == 0)
def ref_t2_fib_15():
    a, b = 1, 1
    for _ in range(13): a, b = b, a + b
    return b
def ref_t2_totient_50(): return sum(1 for k in range(1, 51) if gcd(k, 50) == 1)
def ref_t2_catalan_5(): return comb(10, 5) // 6
def ref_t2_sum_sq_20(): return sum(i*i for i in range(1, 21))
def ref_t2_derang_5():
    return round(math.factorial(5) * sum((-1)**k / math.factorial(k) for k in range(6)))
def ref_t2_det_3x3(): return 1*(0-24) - 2*(0-20) + 3*(0-5)
def ref_t2_mod_power(): return pow(5, 23, 101)
def ref_t2_binom_sum(): return 2**15

# ---------------------------------------------------------------------------
# Tier 3
# ---------------------------------------------------------------------------
def ref_t3_recurrence():
    a = 2
    for n in range(10): a = 2*a + n
    return a
def ref_t3_crt_3mods():
    for x in range(1, 1000):
        if x % 3 == 2 and x % 5 == 3 and x % 7 == 2: return x
def ref_t3_coprime_count():
    return sum(1 for i in range(1, 1001) if all(i % p != 0 for p in (2, 3, 5, 7)))
def ref_t3_stirling2_6_3():
    S = [[0]*7 for _ in range(7)]; S[0][0] = 1
    for n in range(1, 7):
        for k in range(1, 7): S[n][k] = k*S[n-1][k] + S[n-1][k-1]
    return S[6][3]
def ref_t3_hamilton_k5(): return math.factorial(4)
def ref_t3_digit_two_count(): return sum(str(i).count('2') for i in range(1, 1000))
def ref_t3_squarefree_100():
    return sum(1 for i in range(1, 101) if all(i % (p*p) != 0 for p in range(2, 11)))
def ref_t3_int_solutions():
    return sum(1 for x in range(11) for y in range(9) if 0 <= 20-x-y <= 9)
def ref_t3_odd_bin_parity():
    return sum(1 for i in range(1, 128) if bin(i).count('1') % 2 == 1)
def ref_t3_lucas_binom():
    def lucas(n, k, p):
        if k < 0 or k > n: return 0
        res = 1
        while n > 0 or k > 0:
            ni, ki = n % p, k % p
            if ki > ni: return 0
            res = (res * comb(ni, ki)) % p; n //= p; k //= p
        return res
    return lucas(100, 10, 3)

# ---------------------------------------------------------------------------
# Tier 4
# ---------------------------------------------------------------------------
def ref_t4_cpm():
    dur = {'A':3,'B':5,'C':4,'D':6,'E':2,'F':3,'G':4}
    pred = {'A':[],'B':['A'],'C':['A'],'D':['B','C'],'E':['C'],'F':['D','E'],'G':['F']}
    order = ['A','B','C','D','E','F','G']
    ES,EF={},{},
    for a in order:
        ES[a]=max((EF[p] for p in pred[a]),default=0); EF[a]=ES[a]+dur[a]
    T=EF['G']; LS,LF={},{}
    for a in reversed(order):
        succs=[b for b in order if a in pred[b]]
        LF[a]=T if not succs else min(LS[s] for s in succs); LS[a]=LF[a]-dur[a]
    return LS['E']-ES['E']
def ref_t4_burnside_cube(): return (1*3**6+6*3**3+3*3**4+8*3**2+6*3**3)//24
def ref_t4_schroder_6():
    s=[1,1]
    for n in range(1,7): s.append(((6*n-3)*s[n]-(n-2)*s[n-1])//(n+1))
    return s[7]
def ref_t4_multinomial_12(): return math.factorial(12)//(2*6*24*6)
def ref_t4_totient_large():
    n=(2**10)*(3**5)*(5**2)*7
    return int(n*Fraction(1,2)*Fraction(2,3)*Fraction(4,5)*Fraction(6,7))
def ref_t4_menage_6():
    n=6; total=0
    for k in range(n+1):
        t=(-1)**k*math.factorial(n-k)
        if 2*n-k>0: t*=(2*n)*comb(2*n-k,k)//(2*n-k)
        else: continue
        total+=t
    return total
def ref_t4_bipartite_edges(): return 5*7
def ref_t4_walks_k4(): return 7
def ref_t4_partition_15():
    pents=[]
    for k in range(1,20): pents.append((k*(3*k-1))//2); pents.append((k*(3*k+1))//2)
    signs=[1,1,-1,-1]*10; p=[0]*16; p[0]=1
    for n in range(1,16):
        s=0
        for g,sign in zip(pents,signs):
            if g>n: break
            s+=sign*p[n-g]
        p[n]=s
    return p[15]
def ref_t4_cayley_7(): return 7**5

# ===========================================================================
# TIER 5 — HARDENED (8 new + 3 kept = 10)
# ===========================================================================

# --- kept: expected-htth ---
