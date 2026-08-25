from __future__ import annotations

from reasoning_refs_t5 import (
    ref_t5_constrained_partition,
    ref_t5_crt_extra,
    ref_t5_dihedral_bracelets,
    ref_t5_euler_totient_chain,
    ref_t5_expected_htth,
    ref_t5_hex_burnside_freq,
    ref_t5_lattice_path,
    ref_t5_markov_expect_ht,
    ref_t5_stirling_sum,
    ref_t5_surjection_bounded,
    xcheck_t5_constrained_partition,
    xcheck_t5_crt_extra,
    xcheck_t5_dihedral_bracelets,
    xcheck_t5_euler_totient_chain,
    xcheck_t5_expected_htth,
    xcheck_t5_hex_burnside_freq,
    xcheck_t5_lattice_path,
    xcheck_t5_markov_expect_ht,
    xcheck_t5_stirling_sum,
    xcheck_t5_surjection_bounded
)
from reasoning_registry_core import register


def register_items() -> None:
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
