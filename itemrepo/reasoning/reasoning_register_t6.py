from __future__ import annotations

from reasoning_refs_t6 import (
    ref_t6_burnside_s4_pairs,
    ref_t6_coupon_nonuniform,
    ref_t6_crt_coprime_count,
    ref_t6_cube_burnside_freq3,
    ref_t6_derangement_constrained,
    ref_t6_ie_5sets_exactly_2,
    ref_t6_markov_6transient,
    ref_t6_matrix_power_trace,
    ref_t6_mod_cascade_3stage,
    ref_t6_polya_d12_freq3,
    xcheck_t6_burnside_s4_pairs,
    xcheck_t6_coupon_nonuniform,
    xcheck_t6_crt_coprime_count,
    xcheck_t6_cube_burnside_freq3,
    xcheck_t6_derangement_constrained,
    xcheck_t6_ie_5sets_exactly_2,
    xcheck_t6_markov_6transient,
    xcheck_t6_matrix_power_trace,
    xcheck_t6_mod_cascade_3stage,
    xcheck_t6_polya_d12_freq3
)
from reasoning_registry_core import register


def register_items() -> None:
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
