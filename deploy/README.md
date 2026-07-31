# VPS運用メモ

## 構成
- サーバー: シンVPS 162.43.79.204 (Ubuntu 24.04 / 2コア / 2GB / 50GB)
- 実行ユーザー: `tatibana` (rootでは動かさない)
- 場所: `/home/tatibana/tatibana_bot`
- 接続: `ssh -i ~/.ssh/tatibana_vps tatibana@162.43.79.204`

## systemd (launchdの代わり)
| ユニット | 時刻 | 内容 |
|---|---|---|
| tatibana-engine.timer | 平日 8:53 | 場中エンジン起動 |
| tatibana-premarket.timer | 平日 8:13 | 朝のニュース解析 |
| tatibana-nightly.timer | 毎日 21:03 | 銘柄選定・モデル学習 |
| tatibana-report.timer | 平日 15:40 | 日次成績をシートに記入 |
| tatibana-compress.timer | 毎日 23:30 | 板録画をgzip圧縮 |

`Restart=on-failure` なので**異常終了時だけ10秒後に自動再起動**する。
引け(15:30)の正常終了では再起動しない。

## よく使うコマンド
```bash
# 状態を見る
ssh -i ~/.ssh/tatibana_vps root@162.43.79.204 "systemctl status tatibana-engine --no-pager"
ssh -i ~/.ssh/tatibana_vps root@162.43.79.204 "systemctl list-timers 'tatibana-*' --no-pager"

# 手で起動/停止
ssh -i ~/.ssh/tatibana_vps root@162.43.79.204 "systemctl start tatibana-engine"
ssh -i ~/.ssh/tatibana_vps root@162.43.79.204 "systemctl stop tatibana-engine"

# ログ
ssh -i ~/.ssh/tatibana_vps tatibana@162.43.79.204 "tail -50 ~/tatibana_bot/logs/engine.log"

# コードを更新する (GitHubにpush後)
ssh -i ~/.ssh/tatibana_vps tatibana@162.43.79.204 "cd ~/tatibana_bot && git pull && .venv/bin/python -m pytest -q"
```

## **絶対に守ること**
立花APIは **1口座 = 1セッション**。VPSとMacの両方でエンジンを動かすと、
後からログインした方が前のセッションを切る。**必ずどちらか一方だけ。**

データを転送するときは**必ず両方のエンジンを止めてから**。
稼働中に転送すると帳簿が食い違う (2026-07-31に実際に発生)。
