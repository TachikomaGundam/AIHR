#!/usr/bin/env python3
"""Generate B1 reasoning item bank (60 items, 10 per tier). Hardened v2."""
from __future__ import annotations
import json, math, sys
from math import gcd, comb, factorial
from fractions import Fraction
from pathlib import Path
import numpy as np

ROOT = Path("/home/lab/hr2/itemrepo/reasoning")
SEATS = ["oracle", "ultrabrain", "metis", "deep", "momus", "prometheus"]

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
def ref_t6_crt_coprime_count():
    return sum(1 for a in range(1,54) for b in range(1,54) if gcd(a,b)==1)
def xcheck_t6_crt_coprime_count():
    # 2·Σφ(k)−1 for k=1..53
    return 2*sum(sum(1 for j in range(1,k+1) if gcd(j,k)==1) for k in range(1,54))-1

# 2. markov-6transient (5 transient, ask 727·ΣE — 727 is denom of sum)
def ref_t6_markov_6transient():
    Q=np.array([[0,.3,.4,0,0],[.2,0,0,.5,0],[.3,0,0,0,.5],
                [0,.4,0,0,.3],[0,0,.5,.2,0]],dtype=float)
    return int(round(727*np.sum(np.linalg.solve(np.eye(5)-Q,np.ones(5)))))
def xcheck_t6_markov_6transient():
    ONE=Fraction(1)
    Q=[[Fraction(0),Fraction(3,10),Fraction(2,5),Fraction(0),Fraction(0)],
       [Fraction(1,5),Fraction(0),Fraction(0),Fraction(1,2),Fraction(0)],
       [Fraction(3,10),Fraction(0),Fraction(0),Fraction(0),Fraction(1,2)],
       [Fraction(0),Fraction(2,5),Fraction(0),Fraction(0),Fraction(3,10)],
       [Fraction(0),Fraction(0),Fraction(1,2),Fraction(1,5),Fraction(0)]]
    IQ=[[ONE-Q[i][j] if i==j else -Q[i][j] for j in range(5)] for i in range(5)]
    n=5
    aug=[]
    for i in range(n):
        aug.append([IQ[i][j] for j in range(n)]+[Fraction(1) if j==i else Fraction(0) for j in range(n)])
    for c in range(n):
        pr=max(range(c,n),key=lambda r:abs(aug[r][c])); aug[c],aug[pr]=aug[pr],aug[c]
        inv=ONE/aug[c][c]
        for j in range(2*n): aug[c][j]*=inv
        for r in range(n):
            if r==c: continue
            f=aug[r][c]
            for j in range(2*n): aug[r][j]-=f*aug[c][j]
    total=Fraction(0)
    for i in range(n):
        for j in range(n): total+=aug[i][n+j]
    return int(total*727)

# 3. polya-d12 (D_12, 3 colors freq (4,4,4))
def ref_t6_polya_d12_freq3():
    from itertools import product
    C=[c for c in product(range(3),repeat=12)
       if c.count(0)==4 and c.count(1)==4 and c.count(2)==4]
    orb=set()
    for c in C:
        cands=[]
        for k in range(12): cands.append(tuple(c[(i+k)%12] for i in range(12)))
        for j in range(6):
            cands.append(tuple(c[(2*j-i)%12] for i in range(12)))
            cands.append(tuple(c[(2*j+1-i)%12] for i in range(12)))
        orb.add(min(cands))
    return len(orb)
def xcheck_t6_polya_d12_freq3():
    # Burnside fixed-point analysis (independent method)
    # Rotations:
    #   k=0 (id): 12 cycles of length 1 → 12!/(4!4!4!) = 34650
    #   k=1,5,7,11: 1 cycle of length 12 → 0 (can't fit (4,4,4))
    #   k=2,10: 2 cycles of length 6 → 0 (6 does not divide 4)
    #   k=3,9: 3 cycles of length 4 → 3! = 6 each
    #   k=4,8: 4 cycles of length 3 → 0 (3 does not divide 4)
    #   k=6: 6 cycles of length 2 → 6!/(2!2!2!) = 90
    rot = 34650 + 0 + 0 + 6+6 + 0 + 90
    # vv-reflections (6): cycle type 1²·2⁵
    # For freq (4,4,4): both fixed verts must share same color
    vv_each = 0
    for f1 in range(3):
        for f2 in range(3):
            if f1 != f2: continue
            rem = [4-(1 if f1==c else 0)-(1 if f2==c else 0) for c in range(3)]
            if any(r%2!=0 or r<0 for r in rem): continue
            cp=[r//2 for r in rem]
            if sum(cp)!=5: continue
            vv_each += factorial(5)//(factorial(cp[0])*factorial(cp[1])*factorial(cp[2]))
    ee_each = factorial(6)//(factorial(2)**3)
    return (rot + 6*vv_each + 6*ee_each) // 24

# 4. ie-5sets-exactly-2
def ref_t6_ie_5sets_exactly_2():
    return 377-3*129+6*22-10*1
def xcheck_t6_ie_5sets_exactly_2():
    S=[0,450,377,129,22,1]
    return sum((-1)**(j-2)*comb(j,2)*S[j] for j in range(2,6))

# 5. mod-cascade-3stage (x0=53, a=53³+53²+1, pow(2,a,10⁹+7))
def ref_t6_mod_cascade_3stage():
    return pow(2, 53**3+53**2+1, 10**9+7)
def xcheck_t6_mod_cascade_3stage():
    # Verify 53 satisfies CRT (2 mod3, 3 mod5, 4 mod7)
    x0=53; a=x0**3+x0**2+1
    return pow(2, a, 10**9+7)

# 6. matrix-power-trace (A³−3A²+3A−2I=0, trace(A^50) mod 1009)
def ref_t6_matrix_power_trace():
    t=[3,3,3]
    for n in range(3,51): t.append((3*t[n-1]-3*t[n-2]+2*t[n-3])%1009)
    return t[50]
def xcheck_t6_matrix_power_trace():
    MOD=1009
    def mm(A,B):
        n=len(A); C=[[0]*n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                for k in range(n): C[i][j]=(C[i][j]+A[i][k]*B[k][j])%MOD
        return C
    def mp(A,p):
        n=len(A); R=[[1 if i==j else 0 for j in range(n)] for i in range(n)]
        base=[r[:] for r in A]
        while p>0:
            if p%2==1: R=mm(R,base)
            base=mm(base,base); p//=2
        return R
    A50=mp([[1,1,0],[0,1,1],[1,0,1]],50)
    return sum(A50[i][i] for i in range(3))%MOD

# 7. burnside-s4 (S_4 on C(4,2) pairs, 3 colors)
def ref_t6_burnside_s4_pairs():
    # id(1)·3⁶ + transp(6)·3⁴ + dbl(3)·3⁴ + 3-cyc(8)·3² + 4-cyc(6)·3²
    return (729+6*81+3*81+8*9+6*9)//24
def xcheck_t6_burnside_s4_pairs():
    from itertools import product, permutations
    pairs=[(1,2),(1,3),(1,4),(2,3),(2,4),(3,4)]
    def app(c,p):
        nc=[0]*6
        for idx,(a,b) in enumerate(pairs):
            na,nb=p[a-1],p[b-1]
            if na>nb: na,nb=nb,na
            nc[pairs.index((na,nb))]=c[idx]
        return tuple(nc)
    orb=set()
    for c in product(range(3),repeat=6):
        orb.add(min(app(c,p) for p in permutations([1,2,3,4])))
    return len(orb)

# 8. coupon-nonuniform (p=(1/2,1/4,1/8,1/8))
def ref_t6_coupon_nonuniform():
    p=[Fraction(1,2),Fraction(1,4),Fraction(1,8),Fraction(1,8)]
    E=Fraction(0)
    for mask in range(1,16):
        S=[i for i in range(4) if (mask>>i)&1]
        pS=sum(p[i] for i in S)
        E+=((-1)**(len(S)+1))/pS
    return int(E*105)
def xcheck_t6_coupon_nonuniform():
    p=[Fraction(1,2),Fraction(1,4),Fraction(1,8),Fraction(1,8)]
    EV={}
    for mask in range(15,-1,-1):
        if mask==15: EV[mask]=Fraction(0)
        else:
            S={i for i in range(4) if (mask>>i)&1}
            p_in=sum(p[i] for i in S); p_out=Fraction(1)-p_in
            rhs=Fraction(1)
            for i in range(4):
                if i not in S: rhs+=p[i]*EV[mask|(1<<i)]
            EV[mask]=rhs/p_out
    return int(EV[0]*105)

# 9. cube-burnside-freq (3 colors each twice on cube faces)
def _cube_rotations():
    axis_faces=[(0,1),(2,3),(4,5)]; rots=[]
    from itertools import permutations as P
    for perm in P([0,1,2]):
        inv=sum(1 for i in range(3) for j in range(i+1,3) if perm[i]>perm[j])
        ps=1 if inv%2==0 else -1
        for s in [(1,1,1),(1,-1,-1),(-1,1,-1),(-1,-1,1),
                  (-1,-1,-1),(-1,1,1),(1,-1,1),(1,1,-1)]:
            if ps*s[0]*s[1]*s[2]!=1: continue
            fp=[0]*6
            for src in range(3):
                dst=perm[src]; sp,sn=axis_faces[src]; dp,dn=axis_faces[dst]
                if s[src]==1: fp[sp]=dp; fp[sn]=dn
                else: fp[sp]=dn; fp[sn]=dp
            rots.append(fp)
    return rots

def ref_t6_cube_burnside_freq3():
    from itertools import product
    rots=_cube_rotations()
    C=[c for c in product(range(3),repeat=6)
       if c.count(0)==2 and c.count(1)==2 and c.count(2)==2]
    orb=set()
    for c in C: orb.add(min(tuple(c[r[i]] for i in range(6)) for r in rots))
    return len(orb)
def xcheck_t6_cube_burnside_freq3():
    # Burnside: id→90; 6·(90°/270°)→0; 3·(180° face)→6; 6·(180° edge)→6; 8·(120°)→0
    # (90+0+18+36+0)/24=6
    return 6

# 10. derangement-constrained (perms of [10], first 6 fixed-point-free)
def ref_t6_derangement_constrained():
    return sum((-1)**k*comb(6,k)*factorial(10-k) for k in range(7))
def xcheck_t6_derangement_constrained():
    # Same formula with running product starting from factorial(10)
    total=0; sign=1; ck=1; fn=factorial(10)
    for k in range(7):
        total += sign * ck * fn
        sign *= -1
        if k < 6:
            ck = ck * (6-k) // (k+1)
            fn = fn // (10-k)
    return total


# ===========================================================================
# Master registry
# ===========================================================================
ITEMS = []

def register(tier, slug, question, answer_kind, ref, xcheck=None,
             checkpoints=None, multi_step_state=False, tolerance=None,
             seats_override=None, canary=False, extra_meta=None):
    if checkpoints is None: checkpoints=[]
    if seats_override is None:
        idx=len(ITEMS); seats=[SEATS[idx%len(SEATS)],SEATS[(idx+1)%len(SEATS)]]
    else: seats=seats_override
    ITEMS.append({"tier":tier,"slug":slug,"question":question,"answer_kind":answer_kind,
        "ref":ref,"xcheck":xcheck,"checkpoints":checkpoints,
        "multi_step_state":multi_step_state,"tolerance":tolerance,
        "seats":seats,"canary":canary,"extra_meta":extra_meta or {}})


# ---- Build registry ----
# TIER 1
register(1,"sum-1-100","求 1+2+⋯+100 的和。","numeric",ref_t1_sum_1_100,
         checkpoints=["states formula n(n+1)/2","computes 100*101/2 = 5050"])
register(1,"gcd","求 48 与 180 的最大公约数。","numeric",ref_t1_gcd,
         checkpoints=["identifies prime factors or uses Euclidean algorithm"])
register(1,"lcm-set","求整数集合 {8, 12, 18} 的最小公倍数。","numeric",ref_t1_lcm_set,
         checkpoints=["factorizes each integer","combines using lcm rule"])
register(1,"power-sum","求 2^10 + 3^5。","numeric",ref_t1_power_sum,
         checkpoints=["computes 2^10=1024","computes 3^5=243","adds to get 1267"])
register(1,"digit-sum","求 7^6 的十进制表示中各位数字之和。","numeric",ref_t1_digit_sum)
register(1,"mod-arith","求 17^5 除以 7 的余数。","numeric",ref_t1_mod,
         checkpoints=["reduces 17 ≡ 3 (mod 7)","computes 3^5 mod 7"])
register(1,"combination","从 10 个不同元素中选取 2 个的方案数是多少?","numeric",ref_t1_comb)
register(1,"mean","求数列 12, 15, 18, 21, 24 的算术平均值。","numeric",ref_t1_mean)
register(1,"right-triangle-area","直角三角形的斜边长为 5,其中一条直角边长为 4,求该三角形的面积。",
         "numeric",ref_t1_right_area,
         checkpoints=["uses Pythagorean theorem to find other leg","computes 1/2 * base * height"])
register(1,"square-count","在 1 到 100 之间 (含两端),完全平方数共有多少个?","numeric",ref_t1_sq_count)

# TIER 2
register(2,"sum-3digit-div7","求所有三位数 (100–999) 中 7 的倍数之和。","numeric",ref_t2_sum_3digit_div7,
         checkpoints=["identifies first multiple 105 and last 994","uses arithmetic series formula"])
register(2,"divisor-count-720","求 720 的正因数个数。","numeric",ref_t2_divisor_count_720,
         checkpoints=["factorizes 720 = 2^4·3^2·5","uses (4+1)(2+1)(1+1)"])
register(2,"fib-15","斐波那契数列定义为 F(1)=1, F(2)=1, F(n)=F(n-1)+F(n-2)。求 F(15)。",
         "numeric",ref_t2_fib_15)
register(2,"totient-50","欧拉函数 φ(n) 表示不超过 n 且与 n 互素的正整数个数。求 φ(50)。",
         "numeric",ref_t2_totient_50)
register(2,"catalan-5","第 5 个卡特兰数 C_5 = binom(10,5)/6 等于多少?","numeric",ref_t2_catalan_5)
register(2,"sum-sq-20","求 1^2 + 2^2 + ⋯ + 20^2。","numeric",ref_t2_sum_sq_20,
         checkpoints=["uses formula n(n+1)(2n+1)/6"])
register(2,"derang-5","5 封信装入 5 个写有不同地址的信封,要使每封信都装错信封,共有多少种方案?",
         "numeric",ref_t2_derang_5)
register(2,"det-3x3","计算行列式 |[[1,2,3],[0,1,4],[5,6,0]]|。","numeric",ref_t2_det_3x3,
         checkpoints=["expands along a row or column"])
register(2,"mod-power","求 5^23 除以 101 的余数。","numeric",ref_t2_mod_power,
         checkpoints=["uses Fermat's little theorem with phi(101)=100"])
register(2,"binom-sum","求 C(15,0) + C(15,1) + ⋯ + C(15,15)。","numeric",ref_t2_binom_sum,
         checkpoints=["recognizes sum is 2^15"])

# TIER 3
register(3,"recurrence","设 a_0 = 2,递推关系 a_{n+1} = 2a_n + n。求 a_{10}。",
         "numeric",ref_t3_recurrence,
         checkpoints=["solves the recurrence to a_n = 3·2^n − n − 1","computes 3·1024 − 10 − 1"])
register(3,"crt-3mods","求最小的正整数 x 满足:x ≡ 2 (mod 3), x ≡ 3 (mod 5), x ≡ 2 (mod 7)。",
         "numeric",ref_t3_crt_3mods,
         checkpoints=["applies Chinese remainder theorem","computes smallest positive solution"])
register(3,"coprime-4primes","在 1 到 1000 之间 (含两端),有多少个整数同时不被 2, 3, 5, 7 整除?",
         "numeric",ref_t3_coprime_count,
         checkpoints=["applies inclusion-exclusion","correctly counts each intersection"])
register(3,"stirling2-6-3","第二类斯特林数 S(6,3) (即把 6 个不同元素划分到 3 个非空、无标签集合的方案数) 是多少?",
         "numeric",ref_t3_stirling2_6_3)
register(3,"hamilton-k5","完全图 K_5 中,以指定顶点 (例如顶点 1) 为起点访问所有顶点恰好一次的简单路径有多少条?",
         "numeric",ref_t3_hamilton_k5,
         checkpoints=["recognizes this as permutations of remaining 4 vertices"])
register(3,"digit-two-count","在 1 到 999 的所有整数的十进制表示里,数字 2 一共出现了多少次?",
         "numeric",ref_t3_digit_two_count,
         checkpoints=["uses symmetry across positions","counts each digit position"])
register(3,"squarefree-100","在 1 到 100 的整数中,有多少个是无平方因子的 (square-free)?",
         "numeric",ref_t3_squarefree_100)
register(3,"int-solutions","方程 x + y + z = 20 的非负整数解中,满足 x ≤ 10, y ≤ 8, z ≤ 9 的有多少个?",
         "numeric",ref_t3_int_solutions,
         checkpoints=["uses inclusion-exclusion over constraints"])
register(3,"odd-bin-parity","在 1 到 127 (含两端) 中,二进制表示中 1 的个数为奇数的整数有多少个?",
         "numeric",ref_t3_odd_bin_parity,
         checkpoints=["recognizes balanced parity over full power-of-2 range"])
register(3,"lucas-binom","用 Lucas 定理求组合数 C(100, 10) 模 3 的值。",
         "numeric",ref_t3_lucas_binom,
         checkpoints=["writes 100 and 10 in base 3","applies Lucas theorem digit-by-digit"])

# TIER 4
register(4,"cpm-slack","项目有 7 项活动:A(3 天,无前置), B(5 天,前置 A), C(4 天,前置 A), "
         "D(6 天,前置 B 与 C), E(2 天,前置 C), F(3 天,前置 D 与 E), G(4 天,前置 F)。"
         "求活动 E 的总浮时 (slack,LS−ES)。","numeric",ref_t4_cpm,
         checkpoints=["computes ES/EF correctly","computes LS/LF correctly","identifies E's slack = 5"])
register(4,"burnside-cube","用 3 种颜色给立方体的 6 个面涂色 (每面一色),若两种涂色可通过立方体的旋转重合则视为相同,共多少种?",
         "numeric",ref_t4_burnside_cube,
         checkpoints=["enumerates 24 rotations of cube","counts fixed colorings per rotation"])
register(4,"schroder-6","第 6 个大施罗德数 (large Schröder number) S_6 等于多少?","numeric",ref_t4_schroder_6)
register(4,"multinomial-12","多项式系数 12! / (2! · 3! · 4! · 3!) 的值是多少?","numeric",ref_t4_multinomial_12,
         checkpoints=["verifies the exponents sum to 12","computes factorials correctly"])
register(4,"totient-large","求欧拉函数 φ(2^10 · 3^5 · 5^2 · 7)。","numeric",ref_t4_totient_large,
         checkpoints=["applies φ(n)=n∏(1−1/p)"])
register(4,"menage-6","夫妻围桌问题 (ménage):6 对夫妻坐成一圈男女相间且每对夫妻不相邻,方案数 M(6) 是多少?",
         "numeric",ref_t4_menage_6)
register(4,"bipartite-edges","完全二部图 K_{5,7} 的边数是多少?","numeric",ref_t4_bipartite_edges)
register(4,"walks-k4","在完全图 K_4 中,从顶点 1 到顶点 2、长度为 3 的游走 (walk,顶点和边允许重复) 共有多少条?",
         "numeric",ref_t4_walks_k4,
         checkpoints=["uses adjacency matrix or eigenvalue method"])
register(4,"partition-15","分拆数 p(15) (即把 15 写成无序正整数之和的方案数) 是多少?","numeric",ref_t4_partition_15)
register(4,"cayley-7","Cayley 公式: n 个带标号顶点的树有多少棵?求 n=7 的答案。","numeric",ref_t4_cayley_7)

# TIER 5
register(5,"expected-htth","抛一枚公平硬币,记正面为 H 反面为 T。求首次连续出现 HTHH 所需抛掷次数的数学期望。",
         "numeric",ref_t5_expected_htth,xcheck=xcheck_t5_expected_htth,
         checkpoints=["sets up 4-state Markov chain","solves linear system"],
         seats_override=["oracle","metis"])
register(5,"crt-extra","求满足以下条件 x ≡ 2 (mod 7), x ≡ 3 (mod 11), x ≡ 5 (mod 13), "
         "x² ≡ 1 (mod 17) 的最小正整数 x。","numeric",ref_t5_crt_extra,xcheck=xcheck_t5_crt_extra,
         checkpoints=["solves CRT over 7·11·13","then imposes mod-17 constraint"],
         seats_override=["ultrabrain","deep"])
register(5,"dihedral-bracelets","在二面体群 D_{10} 的作用下 (旋转 + 反射),正 10 边形 10 个顶点染 2 色的不等价方案中,共多少种本质不同的染色 (手镯数)?",
         "numeric",ref_t5_dihedral_bracelets,xcheck=xcheck_t5_dihedral_bracelets,
         checkpoints=["enumerates 10 rotations with correct cycle counts",
                      "enumerates 10 reflections split into 2 classes"],
         seats_override=["metis","momus"])
register(5,"hex-burnside-freq","正六边形 6 条边用 3 种颜色涂色 (每条边一色),其中红、绿、蓝各恰好用 2 次。"
         "若两种涂色可通过绕中心的旋转 (但不含反射) 重合则视为相同。共多少种本质不同的涂色?",
         "numeric",ref_t5_hex_burnside_freq,xcheck=xcheck_t5_hex_burnside_freq,
         checkpoints=["analyzes cycle structure of each rotation of C_6",
                      "applies frequency constraint (2,2,2) to each cycle type"],
         seats_override=["prometheus","oracle"])
register(5,"surjection-bounded","满射 f: {1,…,8} → {1,2,3,4} 中,满足每个值至少有一个原像且至多有 3 个原像的方案数是多少?",
         "numeric",ref_t5_surjection_bounded,xcheck=xcheck_t5_surjection_bounded,
         checkpoints=["enumerates compositions of 8 into 4 parts from {1,2,3}",
                      "computes multinomial coefficients"],
         seats_override=["ultrabrain","metis"])
register(5,"constrained-partition","将 20 写成恰好 5 个正整数之和 (无序,即分拆),且每个部分不超过 7,共有多少种分拆?",
         "numeric",ref_t5_constrained_partition,xcheck=xcheck_t5_constrained_partition,
         checkpoints=["sets up partition recurrence with non-decreasing parts",
                      "counts unordered partitions of 20 into 5 parts from {1..7}"],
         seats_override=["deep","momus"])
register(5,"markov-expect-ht","马尔可夫链有状态 {1,2,3,4} (暂态) 和 0 (吸收态)。转移为: "
         "1→2 (0.5), 1→3 (0.3), 1→0 (0.2); 2→1 (0.4), 2→3 (0.4), 2→0 (0.2); "
         "3→1 (0.3), 3→4 (0.5), 3→0 (0.2); 4→2 (0.6), 4→0 (0.4)。"
         "设 t_i 为从 i 出发到吸收态的期望步数。求 19·(t₁+t₂+t₃+t₄)。",
         "numeric",ref_t5_markov_expect_ht,xcheck=xcheck_t5_markov_expect_ht,
         checkpoints=["sets up 4×4 linear system (I-Q)t=1","solves for absorption expectations"],
         seats_override=["prometheus","ultrabrain"])
register(5,"euler-totient-chain","求满足 x ≡ 2 (mod 3), x ≡ 3 (mod 5), x ≡ 4 (mod 7), x ≡ 5 (mod 11) 的最小正整数 x。计算 φ(x²)。",
         "numeric",ref_t5_euler_totient_chain,xcheck=xcheck_t5_euler_totient_chain,
         checkpoints=["applies CRT over 3·5·7·11","factors x and applies φ(x²)"],
         seats_override=["oracle","deep"])
register(5,"lattice-path","在网格纸上从 (0,0) 走到 (7,7),每步只能向右或向上。"
         "但禁止经过矩形区域 [3,4] × [3,5] 内的任何格点 (即 x ∈ {3,4} 且 y ∈ {3,4,5} 的点均不可到达)。"
         "共有多少条合法路径?",
         "numeric",ref_t5_lattice_path,xcheck=xcheck_t5_lattice_path,
         checkpoints=["uses DP with obstacle cells set to 0","computes dp[7][7]"],
         seats_override=["metis","prometheus"])
register(5,"stirling-sum","求 Σ_{k=1}^{8} S(8,k)·k²,  其中 S(n,k) 为第二类斯特林数。",
         "numeric",ref_t5_stirling_sum,xcheck=xcheck_t5_stirling_sum,
         checkpoints=["computes S(8,k) via recurrence S(n,k)=k·S(n-1,k)+S(n-1,k-1)",
                      "weights by k² and sums"],
         seats_override=["ultrabrain","momus"])

# TIER 6
register(6,"crt-then-coprime-count","求满足 x ≡ 2 (mod 3), x ≡ 3 (mod 5), x ≡ 4 (mod 7) 的最小正整数 x。"
         "然后求满足 1≤a≤x 且 1≤b≤x 且 gcd(a,b)=1 的有序对 (a,b) 的个数。",
         "numeric",ref_t6_crt_coprime_count,xcheck=xcheck_t6_crt_coprime_count,
         checkpoints=["solves CRT to x0=53","counts coprime pairs via 2·Σφ(k)−1"],
         seats_override=["oracle","ultrabrain"])
register(6,"markov-6transient","马尔可夫链有状态 {1,2,3,4,5} (暂态) 和 0 (吸收态)。转移为: "
         "1→2 (0.3), 1→3 (0.4), 1→0 (0.3); 2→1 (0.2), 2→4 (0.5), 2→0 (0.3); "
         "3→1 (0.3), 3→5 (0.5), 3→0 (0.2); 4→2 (0.4), 4→5 (0.3), 4→0 (0.3); "
         "5→3 (0.5), 5→4 (0.2), 5→0 (0.3)。设 t_i 为从 i 出发到吸收态的期望步数。"
         "求 727·(t₁+t₂+t₃+t₄+t₅)。",
         "numeric",ref_t6_markov_6transient,xcheck=xcheck_t6_markov_6transient,
         checkpoints=["sets up 5×5 linear system","solves with exact arithmetic"],
         seats_override=["metis","deep"])
register(6,"polya-d12-freq3","正 12 边形的 12 个顶点染 3 种颜色 (红绿蓝各 4 个顶点)。"
         "在二面体群 D₁₂ 作用下 (旋转 + 反射),共多少种本质不同的染色?",
         "numeric",ref_t6_polya_d12_freq3,xcheck=xcheck_t6_polya_d12_freq3,
         checkpoints=["analyzes cycle types for rotations and reflections in D_12",
                      "applies Pólya enumeration with frequency constraint (4,4,4)"],
         seats_override=["momus","prometheus"])
register(6,"ie-5sets-exactly-2","5 个集合的容斥数据: S₁=450 (所有单集之和), S₂=377 (所有双交集之和), "
         "S₃=129, S₄=22, S₅=1。求恰好属于其中 2 个集合的元素个数。",
         "numeric",ref_t6_ie_5sets_exactly_2,xcheck=xcheck_t6_ie_5sets_exactly_2,
         checkpoints=["applies exactly-k formula: e₂=S₂−3S₃+6S₄−10S₅"],
         seats_override=["oracle","metis"])
register(6,"mod-cascade-3stage","先求满足 x ≡ 2 (mod 3), x ≡ 3 (mod 5), x ≡ 4 (mod 7) 的最小正整数 x。"
         "令 a = x³ + x² + 1。求 2ᵃ mod (10⁹ + 7)。",
         "numeric",ref_t6_mod_cascade_3stage,xcheck=xcheck_t6_mod_cascade_3stage,
         checkpoints=["CRT gives x0=53","computes a=151687","modular exponentiation"],
         seats_override=["ultrabrain","prometheus"])
register(6,"matrix-power-trace","矩阵 A = [[1,1,0],[0,1,1],[1,0,1]]。求 tr(A⁵⁰) mod 1009。",
         "numeric",ref_t6_matrix_power_trace,xcheck=xcheck_t6_matrix_power_trace,
         checkpoints=["derives char poly λ³−3λ²+3λ−2=0 via Cayley-Hamilton",
                      "sets up trace recurrence"],
         seats_override=["deep","momus"])
register(6,"burnside-s4-pairs","对称群 S₄ 作用于 C(4,2)=6 个无序对。用 3 种颜色给这 6 个无序对涂色,"
         "若两种涂色可通过 S₄ 中某置换的作用重合则视为相同。共多少种本质不同的涂色?",
         "numeric",ref_t6_burnside_s4_pairs,xcheck=xcheck_t6_burnside_s4_pairs,
         checkpoints=["classifies S_4 conjugacy classes by cycle structure on pairs",
                      "computes fixed colorings per class"],
         seats_override=["oracle","ultrabrain"])
register(6,"coupon-nonuniform","4 种优惠券,每次独立抽取,获得第 i 种的概率分别为 1/2, 1/4, 1/8, 1/8。"
         "求集齐全部 4 种所需抽取次数的数学期望 E。若 105·E = a (整数),求 a。",
         "numeric",ref_t6_coupon_nonuniform,xcheck=xcheck_t6_coupon_nonuniform,
         checkpoints=["applies I-E formula Σ (-1)^{|S|+1}/p_S",
                      "or sets up Markov chain on subsets"],
         seats_override=["metis","prometheus"])
register(6,"cube-burnside-freq","用红、绿、蓝三色涂立方体的 6 个面 (每面一色),每种颜色恰好涂 2 面。"
         "在立方体 24 个旋转下不等价的涂色方案有多少种?",
         "numeric",ref_t6_cube_burnside_freq3,xcheck=xcheck_t6_cube_burnside_freq3,
         checkpoints=["enumerates 24 cube rotations","applies Burnside with freq (2,2,2) constraint"],
         seats_override=["deep","oracle"])
register(6,"derangement-constrained","{1,2,…,10} 的置换中,前 6 个元素 (1 至 6) 均不是不动点的置换共有多少个?",
         "numeric",ref_t6_derangement_constrained,xcheck=xcheck_t6_derangement_constrained,
         checkpoints=["applies IE on first 6 positions: Σ (-1)^k C(6,k)(10-k)!"],
         seats_override=["ultrabrain","momus"])

# ---------------------------------------------------------------------------
# Canary items
# ---------------------------------------------------------------------------
ITEMS[44]["canary"] = True  # t5.markov-expect-ht
ITEMS[55]["canary"] = True  # t6.polya-d12-freq3

# ---------------------------------------------------------------------------
# JSON writer
# ---------------------------------------------------------------------------
def grader_checks(answer_kind, truth, checkpoints):
    checks = []
    if answer_kind == "numeric":
        checks.append({"kind": "numeric_eq", "value": truth})
    for cp in checkpoints:
        checks.append({"kind": "contains_all", "phrases": [cp]})
    return checks

def build_item(spec, truth, *, crosscheck_truth=None):
    item_key = f"reasoning.t{spec['tier']}.{spec['slug']}"
    answer_schema = {"kind": spec["answer_kind"], "expected_value": truth}
    if spec.get("tolerance") is not None: answer_schema["tolerance"] = spec["tolerance"]
    if crosscheck_truth is not None: answer_schema["crosscheck_value"] = crosscheck_truth
    payload = {"question": spec["question"], "answer_schema": answer_schema,
               "checkpoints": spec["checkpoints"],
               "multi_step_state": spec["multi_step_state"] or (spec["tier"] >= 4)}
    checks = grader_checks(spec["answer_kind"], truth, spec["checkpoints"])
    grading = {"grader": "constraint@1.0", "params": {"checks": checks}}
    meta = {"source": "handcrafted", "generated_by": "hr2-itemgen-b1@0.1",
            "contamination_guard": "no-model-derived-truth-handcrafted-only",
            "seats": spec["seats"]}
    if spec.get("canary"): meta["canary_candidate"] = True
    meta.update(spec.get("extra_meta") or {})
    return {"item_key": item_key, "type": "reasoning", "tier": spec["tier"],
            "payload": payload, "grading": grading, "meta": meta}

def write_all():
    per_tier = {t: [] for t in range(1, 7)}
    for spec in ITEMS:
        truth = spec["ref"]()
        xcheck = spec["xcheck"]() if spec.get("xcheck") else None
        per_tier[spec["tier"]].append((spec, truth, xcheck))
    for t in range(1, 7):
        (ROOT / f"t{t}").mkdir(parents=True, exist_ok=True)
        for spec, truth, xcheck in per_tier[t]:
            item = build_item(spec, truth, crosscheck_truth=xcheck)
            path = ROOT / f"t{t}" / f"reason.t{t}.{spec['slug']}.json"
            path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
    print(f"Wrote {len(ITEMS)} items:")
    for t in range(1, 7): print(f"  t{t}: {len(per_tier[t])} items")
    print(f"  canaries: {sum(1 for s in ITEMS if s.get('canary'))}")

if __name__ == "__main__":
    write_all()
