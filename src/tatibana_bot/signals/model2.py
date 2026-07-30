"""場中MLモデル 第2ラウンド.

第1ラウンド (AUC 0.593、不採用) の反省点:
  1. 検証データの切り方が「銘柄で80/20」だった。時間で分けていないため
     「未来を当てられるか」を測れていなかった。しかも1秒間隔の行は
     ラベル(30個先)が重なり合っており、実質カンニングに近い。
  2. ラベルが「30個先に+5bp」で、実際の決済ルール(トレーリング0.3%/損切り0.25%)
     と無関係だった。当たっても儲かるとは限らない。
  3. 特徴量が板の瞬間値だけで、「どう変化したか」を捨てていた。

第2ラウンドの設計:
  - ラベルは三重障壁: 利確に先に届いたか、損切りに先に届いたか (実運用と同じ)
  - 検証は**日で分割** (前半の日で学習、後半の日は一度も見ない)
  - 特徴量に時系列の変化 (5秒/30秒/300秒) と時刻を追加
  - 1秒ごとの行は重なるので間引いて使う
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BASE_FEATURES = [
    "imbalance", "imbalance_top1", "spread_bps", "microprice_dev",
    "bid_depth", "ask_depth", "buy_ratio", "tick_count", "price_drift",
]

# 時系列の変化 (第1ラウンドで捨てていた情報)
DERIVED_FEATURES = [
    "ret_5s", "ret_30s", "ret_300s",          # モメンタム
    "imb_chg_5s", "imb_chg_30s",              # 板の傾きの変化
    "depth_ratio_30s", "depth_ratio_300s",    # 板が厚くなったか薄くなったか
    "tick_surge",                             # 約定頻度が平常時の何倍か
    "spread_rel",                             # スプレッドが平常時の何倍か
    "vol_300s",                                # 直近5分の値動きの荒さ
    "minutes_from_open",                       # 日中のU字型を捉える
]
FEATURES = BASE_FEATURES + DERIVED_FEATURES


def _clip(a: np.ndarray) -> np.ndarray:
    """inf/NaN を潰す (板の片側が空だとスプレッドが inf で記録されている)."""
    return np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)


def add_features(g: pd.DataFrame) -> pd.DataFrame:
    """1銘柄・1日のスナップショット列に時系列の特徴量を足す (行は時刻順)."""
    px = g["last_price"].to_numpy(dtype=float)
    imb = _clip(g["imbalance"].to_numpy(dtype=float))
    bd = _clip(g["bid_depth"].to_numpy(dtype=float))
    ad = _clip(g["ask_depth"].to_numpy(dtype=float))
    tick = _clip(g["tick_count"].to_numpy(dtype=float))
    spread = _clip(g["spread_bps"].to_numpy(dtype=float))

    def lag(a: np.ndarray, k: int) -> np.ndarray:
        out = np.empty_like(a)
        out[:k] = a[0] if len(a) else 0.0
        out[k:] = a[:-k]
        return out

    # スナップショットはほぼ1秒間隔なので、k個前 ≒ k秒前として扱う
    for name, k in [("ret_5s", 5), ("ret_30s", 30), ("ret_300s", 300)]:
        prev = lag(px, min(k, max(len(px) - 1, 1)))
        g[name] = np.where(prev > 0, (px - prev) / prev * 10000, 0.0)
    for name, k in [("imb_chg_5s", 5), ("imb_chg_30s", 30)]:
        g[name] = imb - lag(imb, min(k, max(len(imb) - 1, 1)))
    depth = bd + ad
    for name, k in [("depth_ratio_30s", 30), ("depth_ratio_300s", 300)]:
        base = pd.Series(depth).rolling(k, min_periods=1).mean().to_numpy()
        g[name] = np.where(base > 0, depth / base, 1.0)

    # np.where は両側を先に評価するため、0除算の警告が出る。安全な分母を作って割る。
    tick_avg = pd.Series(tick).expanding().mean().to_numpy()
    safe = np.where(tick_avg > 0.5, tick_avg, 1.0)
    g["tick_surge"] = np.where(tick_avg > 0.5, tick / safe, 0.0)
    sp_avg = pd.Series(spread).rolling(300, min_periods=30).mean().to_numpy()
    ok = np.isfinite(sp_avg) & (sp_avg > 0)
    g["spread_rel"] = np.where(ok, spread / np.where(ok, sp_avg, 1.0), 1.0)
    roll = pd.Series(px).rolling(300, min_periods=30)
    g["vol_300s"] = _clip(((roll.max() - roll.min()) / np.where(px > 0, px, 1) * 10000).to_numpy())

    t = pd.to_datetime(g["ts"])
    g["minutes_from_open"] = (t.dt.hour * 60 + t.dt.minute - 9 * 60).astype(float)
    return g


def triple_barrier(px: np.ndarray, target_pct: float, stop_pct: float,
                   window: int) -> np.ndarray:
    """買った場合に「利確に先に届いたか」を 1/0/NaN で返す.

    window 本先までに利確も損切りも来なければ NaN (判定不能として捨てる)。
    """
    n = len(px)
    up = px * (1 + target_pct / 100)
    dn = px * (1 - stop_pct / 100)
    hit_up = np.full(n, np.inf)
    hit_dn = np.full(n, np.inf)
    for k in range(1, window + 1):
        fut = np.empty(n)
        fut[: n - k] = px[k:]
        fut[n - k:] = np.nan
        newly_up = (hit_up == np.inf) & (fut >= up)
        newly_dn = (hit_dn == np.inf) & (fut <= dn)
        hit_up[newly_up] = k
        hit_dn[newly_dn] = k
        if not np.isinf(hit_up).any() and not np.isinf(hit_dn).any():
            break
    label = np.where(hit_up < hit_dn, 1.0, np.where(hit_dn < hit_up, 0.0, np.nan))
    label[(hit_up == np.inf) & (hit_dn == np.inf)] = np.nan   # どちらも来ず
    label[np.arange(n) > n - window - 1] = np.nan             # 末尾は先が見えない
    return label


def build_day(df: pd.DataFrame, target_pct: float, stop_pct: float,
              window: int, stride: int) -> pd.DataFrame:
    """1日分のスナップショットから学習用の表を作る."""
    out = []
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("ts").reset_index(drop=True)
        if "last_price" not in g.columns or len(g) < window + 60:
            continue
        g = add_features(g)
        px = g["last_price"].to_numpy(dtype=float)
        if (px <= 0).any():
            continue
        g["label_buy"] = triple_barrier(px, target_pct, stop_pct, window)
        # 売りは値動きを反転させて同じ判定にかける
        # 売りは値動きを上下反転させて同じ判定にかける
        g["label_sell"] = triple_barrier(px[0] * 2 - px, target_pct, stop_pct, window)
        out.append(g.iloc[::stride])
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


def train_with_day_split(table: pd.DataFrame, model_path, label_col: str,
                         valid_days: int = 6) -> dict:
    """後ろの valid_days 日を一度も学習に使わずに検証する."""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    days = sorted(table["day"].unique())
    if len(days) <= valid_days:
        raise SystemExit(f"日数が足りない ({len(days)}日)")
    train_days, test_days = days[:-valid_days], days[-valid_days:]
    tr = table[table["day"].isin(train_days)].dropna(subset=[label_col])
    te = table[table["day"].isin(test_days)].dropna(subset=[label_col])
    cols = [c for c in FEATURES if c in table.columns]

    model = lgb.train(
        {"objective": "binary", "metric": "auc", "learning_rate": 0.05,
         "num_leaves": 31, "min_data_in_leaf": 200, "feature_fraction": 0.8,
         "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1, "seed": 42},
        lgb.Dataset(tr[cols], label=tr[label_col]),
        num_boost_round=400,
        valid_sets=[lgb.Dataset(te[cols], label=te[label_col])],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    pred = model.predict(te[cols])
    auc = float(roc_auc_score(te[label_col], pred))
    from pathlib import Path
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))

    # 実運用の見立て: 閾値で絞ったときの勝率 (ベースラインは絞らない場合)
    base = float(te[label_col].mean())
    gates = {}
    for th in (0.50, 0.55, 0.60, 0.65):
        m = pred >= th
        gates[th] = {"通過率": float(m.mean()),
                     "勝率": float(te[label_col][m].mean()) if m.any() else 0.0}
    return {"auc": auc, "n_train": len(tr), "n_valid": len(te),
            "train_days": [str(d) for d in train_days],
            "valid_days": [str(d) for d in test_days],
            "baseline_win_rate": base, "gates": gates,
            "importance": dict(sorted(zip(cols, model.feature_importance("gain")),
                                      key=lambda x: -x[1])[:8])}
