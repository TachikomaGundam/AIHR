from __future__ import annotations

from fractions import Fraction
from math import factorial, gcd

import numpy as np

def ref_t5_expected_htth(): return 18
def xcheck_t5_expected_htth():
    ONE=Fraction(1); H=Fraction(1,2)
    M=[[H,-H,0,0,ONE],[0,H,-H,0,ONE],[-H,0,ONE,-H,ONE],[0,0,-H,ONE,ONE]]
    for c in range(4):
        pr=max(range(c,4),key=lambda r:abs(M[r][c])); M[c],M[pr]=M[pr],M[c]
        for r in range(c+1,4):
            f=M[r][c]/M[c][c]
            for j in range(c,5): M[r][j]-=f*M[c][j]
    t=[Fraction(0)]*4
    for i in range(3,-1,-1):
        s=M[i][4]
        for j in range(i+1,4): s-=M[i][j]*t[j]
        t[i]=s/M[i][i]
    return int(t[0])

# --- kept: crt-extra ---
def ref_t5_crt_extra():
    for x in range(1,50000):
        if x%7==2 and x%11==3 and x%13==5 and (x*x)%17==1: return x
def xcheck_t5_crt_extra():
    x0=2*143*5+3*91*4+5*77*12; x0%=1001
    for k in range(100):
        x=x0+1001*k
        if (x*x)%17==1: return x

# --- demoted from t6: dihedral-bracelets ---
def ref_t5_dihedral_bracelets():
    return (sum(2**gcd(k,10) for k in range(10))+5*(2**6)+5*(2**5))//20
def xcheck_t5_dihedral_bracelets(): return 78

# --- new: hex-burnside-freq (C_6, 3 colors freq (2,2,2)) ---
def ref_t5_hex_burnside_freq(): return (90+6)//6  # Burnside: {id:90, r³:6}/6
def xcheck_t5_hex_burnside_freq():
    from itertools import product
    C=[c for c in product(range(3),repeat=6) if c.count(0)==2 and c.count(1)==2 and c.count(2)==2]
    orb=set()
    for c in C: orb.add(min(tuple(c[(i+k)%6] for i in range(6)) for k in range(6)))
    return len(orb)

# --- new: surjection-bounded ([8]→[4] with |f⁻¹(i)|≤3) ---
def ref_t5_surjection_bounded():
    return sum(factorial(8)//(factorial(a)*factorial(b)*factorial(c)*factorial(8-a-b-c))
               for a in range(1,4) for b in range(1,4) for c in range(1,4)
               if 1<=8-a-b-c<=3)
def xcheck_t5_surjection_bounded():
    # Generating function: coeff of x^8 in 8!·(x+x²/2!+x³/3!)^4
    poly=[Fraction(0),Fraction(1),Fraction(1,2),Fraction(1,6)]
    res=[Fraction(1)]
    for _ in range(4):
        nr=[Fraction(0)]*(len(res)+len(poly)-1)
        for i,a in enumerate(res):
            for j,b in enumerate(poly): nr[i+j]+=a*b
        res=nr
    return int(res[8]*factorial(8))

# --- new: constrained-partition (unordered partitions of 20 into 5 parts ≤7) ---
def ref_t5_constrained_partition():
    memo = {}
    def f(n, k, lo, hi):
        if k == 0: return 1 if n == 0 else 0
        if n < k * lo or n > k * hi: return 0
        if k == 1: return 1 if lo <= n <= hi else 0
        if (n, k, lo, hi) in memo: return memo[(n,k,lo,hi)]
        ans = f(n-lo, k-1, lo, hi) + f(n-k, k, lo, hi-1)
        memo[(n,k,lo,hi)] = ans
        return ans
    return f(20, 5, 1, 7)
def xcheck_t5_constrained_partition():
    from itertools import combinations_with_replacement
    return sum(1 for p in combinations_with_replacement(range(1,8),5) if sum(p)==20)

# --- new: markov-expect-ht (4 transient states, ask 19·ΣE) ---
def ref_t5_markov_expect_ht():
    Q=np.array([[0,.5,.3,0],[.4,0,.4,0],[.3,0,0,.5],[0,.6,0,0]],dtype=float)
    return int(round(19*np.sum(np.linalg.solve(np.eye(4)-Q,np.ones(4)))))
def xcheck_t5_markov_expect_ht():
    ONE=Fraction(1)
    Q=[[Fraction(0),Fraction(1,2),Fraction(3,10),Fraction(0)],
       [Fraction(2,5),Fraction(0),Fraction(2,5),Fraction(0)],
       [Fraction(3,10),Fraction(0),Fraction(0),Fraction(1,2)],
       [Fraction(0),Fraction(3,5),Fraction(0),Fraction(0)]]
    M=[[ONE-Q[i][j] if i==j else -Q[i][j] for j in range(4)]+[ONE] for i in range(4)]
    for c in range(4):
        pr=max(range(c,4),key=lambda r:abs(M[r][c])); M[c],M[pr]=M[pr],M[c]
        for r in range(c+1,4):
            f=M[r][c]/M[c][c]
            for j in range(c,5): M[r][j]-=f*M[c][j]
    t=[Fraction(0)]*4
    for i in range(3,-1,-1):
        s=M[i][4]
        for j in range(i+1,4): s-=M[i][j]*t[j]
        t[i]=s/M[i][i]
    return int(sum(t)*19)

# --- new: euler-chain (CRT → φ(x0²)) ---
# x≡2 mod3, 3 mod5, 4 mod7, 5 mod11 → x0=368=2⁴·23 → φ(x0²)=64768
def ref_t5_euler_totient_chain():
    x0=368; n=x0*x0; phi=n; temp=x0; p=2
    while p*p<=temp:
        if temp%p==0:
            phi-=phi//p
            while temp%p==0: temp//=p
        p+=1
    if temp>1: phi-=phi//temp
    return phi
def xcheck_t5_euler_totient_chain():
    # 368²=2⁸·23²; φ=2⁸·23²·(1/2)·(22/23)=128·23·22
    return 128*23*22

# --- new: lattice-path (0,0→7,7 avoiding [3,4]×[3,5]) ---
def ref_t5_lattice_path():
    dp=[[0]*8 for _ in range(8)]; dp[0][0]=1
    for i in range(8):
        for j in range(8):
            if i==0 and j==0: continue
            if 3<=i<=4 and 3<=j<=5: continue
            if i>0: dp[i][j]+=dp[i-1][j]
            if j>0: dp[i][j]+=dp[i][j-1]
    return dp[7][7]
def xcheck_t5_lattice_path():
    # Column-major DP
    dp=[[0]*8 for _ in range(8)]; dp[0][0]=1
    for j in range(8):
        for i in range(8):
            if i==0 and j==0: continue
            if 3<=i<=4 and 3<=j<=5: continue
            if i>0: dp[i][j]+=dp[i-1][j]
            if j>0: dp[i][j]+=dp[i][j-1]
    return dp[7][7]

# --- new: stirling-sum (Σ S(8,k)·k²) ---
def ref_t5_stirling_sum():
    S=[[0]*9 for _ in range(9)]; S[0][0]=1
    for n in range(1,9):
        for k in range(1,9): S[n][k]=k*S[n-1][k]+S[n-1][k-1]
    return sum(S[8][k]*k*k for k in range(1,9))
def xcheck_t5_stirling_sum():
    return sum(v*k*k for k,v in enumerate([0,1,127,966,1701,1050,266,28,1]))

# ===========================================================================
# TIER 6 — HARDENED (10 new items)
# ===========================================================================

# 1. crt-then-count (x0=53, coprime pairs ≤53)
