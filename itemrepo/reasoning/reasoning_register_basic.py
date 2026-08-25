from __future__ import annotations

from reasoning_refs_basic import (
    ref_t1_comb,
    ref_t1_digit_sum,
    ref_t1_gcd,
    ref_t1_lcm_set,
    ref_t1_mean,
    ref_t1_mod,
    ref_t1_power_sum,
    ref_t1_right_area,
    ref_t1_sq_count,
    ref_t1_sum_1_100,
    ref_t2_binom_sum,
    ref_t2_catalan_5,
    ref_t2_derang_5,
    ref_t2_det_3x3,
    ref_t2_divisor_count_720,
    ref_t2_fib_15,
    ref_t2_mod_power,
    ref_t2_sum_3digit_div7,
    ref_t2_sum_sq_20,
    ref_t2_totient_50,
    ref_t3_coprime_count,
    ref_t3_crt_3mods,
    ref_t3_digit_two_count,
    ref_t3_hamilton_k5,
    ref_t3_int_solutions,
    ref_t3_lucas_binom,
    ref_t3_odd_bin_parity,
    ref_t3_recurrence,
    ref_t3_squarefree_100,
    ref_t3_stirling2_6_3,
    ref_t4_bipartite_edges,
    ref_t4_burnside_cube,
    ref_t4_cayley_7,
    ref_t4_cpm,
    ref_t4_menage_6,
    ref_t4_multinomial_12,
    ref_t4_partition_15,
    ref_t4_schroder_6,
    ref_t4_totient_large,
    ref_t4_walks_k4
)
from reasoning_registry_core import register


def register_items() -> None:
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
