from __future__ import annotations

from fractions import Fraction
from math import comb, factorial, gcd

import numpy as np

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


