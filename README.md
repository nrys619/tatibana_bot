# tatibana_bot

立花証券 e支店 API を使った日本株AIデイトレボット。

AIは「場中の高速判断」ではなく「選別と解釈」に使う設計:

| # | モジュール | 役割 |
|---|---|---|
| ① | `screening/` | **銘柄スクリーニングML** — 日足特徴量 + LightGBM で「翌日デイトレ向きに動く銘柄」をランキングし監視リストを生成 |
| ② | `disclosure/` | **LLM開示解析** — TDnet適時開示を Claude API (structured outputs) で好悪判定・インパクト評価し監視リストに反映 |
| ③ | `signals/` | **板・歩み値シグナル** — 板の不均衡/マイクロプライス + 歩み値の買い圧力によるルールベース戦略。スナップショットを収集して短期MLで強化可能 |
| ④ | `risk/` | **リスク管理** — 指数ベースの地合い(レジーム)判定、固定リスク率サイジング、板/価格の異常検知、日次キルスイッチ |
|   | `api/` `engine/` | 立花APIクライアントと場中実行エンジン (paper/live) |

## セットアップ

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                              # 認証情報を記入
cp config/config.example.yaml config/config.yaml  # パラメータ調整
```

必要な認証情報 (`.env` または環境変数):

- **立花証券e支店 — 公開鍵認証 (v4r9〜, 推奨)**
  会員ページで発行した `e_api_authid.txt` (認証ID) と `e_api_private_key.pem` (秘密鍵) を
  リポジトリ外か `secrets/` (gitignore済み) に置き、
  `TACHIBANA_AUTH_ID_FILE` / `TACHIBANA_PRIVATE_KEY_FILE` にパスを設定。
  公開鍵 (`e_api_public_key.pem/.der`) は立花側に登録するもので、ボットの実行には不要。
  **秘密鍵と認証IDは絶対にgitにコミットしないこと** (`.gitignore` で `*.pem` `*.der`
  `*_authid.txt` `secrets/` をブロック済み)。デモと本番で鍵セットは別物。
- 旧パスワード認証 (v4r8以前) を使う場合は `config.yaml` の `api.auth_method: password` と
  `TACHIBANA_USER_ID` / `TACHIBANA_PASSWORD`
- `ANTHROPIC_API_KEY` — 開示解析(②)に使用

公開鍵認証のログインは、レスポンスの仮想URLが口座登録済みの公開鍵でRSA暗号化
(OAEP/SHA-256 + Base64) されて返るため、手元の秘密鍵で復号する方式です
(`api/crypto.py`、[公式Pythonサンプル](https://github.com/e-shiten-jp)準拠)。

## 1日の運用フロー

```bash
# 引け後〜夜間: 日足更新 + スクリーニング (--train で再学習)
python scripts/nightly.py --train

# 寄り前 (8時台): 前日の開示をLLM解析して監視リストに反映
python scripts/premarket.py

# 場中: エンジン起動 (デフォルトは paper = 発注しない)
python scripts/run_live.py --mode paper

# (任意) 板スナップショットが数週間たまったら③の短期MLを学習
python scripts/train_intraday.py
```

`run_live.py` は監視リスト銘柄の板を1秒間隔でポーリングし、

1. ④異常検知 (板消失・流動性蒸発・急変動) — 異常時は新規停止/保有は即決済
2. ④キルスイッチ (日次損失上限・連敗数) チェック
3. ③シグナル判定 (板不均衡 + 歩み値 + スプレッドフィルタ ± MLスコア)
4. ②の開示センチメントと逆方向のエントリーをブロック
5. ④レジーム倍率 × 確信度でサイジングして執行 (paper/live)

全シグナル・約定は `data/trades.sqlite3` に記録され、成績分析と再学習に使えます。

## 実運用前の必須事項

- **必ずデモ環境 (`api.env: demo`) と paper モードから始めること。**
- 立花証券のAPI仕様書 (最新版) と突き合わせて `config.yaml` の `api.version`、
  `api/session.py`・`api/orders.py`・`api/client.py` の電文名 (`sCLMID`) と
  時価カラム名 (`pGBP1` 等) を検証すること。仕様書の版により名称が変わります。
- 日足データはデフォルトで yfinance (非公式)。本運用では J-Quants API 等への差し替えを推奨。
- TDnet取得は非公式のコミュニティAPI (webapi.yanoshin.jp) を利用。同様に差し替え可能な設計。
- バックテスト・フォワードテストで期待値がプラスであることを確認するまで実弾を入れないこと。
  スリッページと手数料で紙上の優位性は容易に消えます。

## LLM解析のコストについて

②はデフォルトで `claude-opus-4-8` (高精度) を使用します。開示件数が多い場合は
`config.yaml` の `disclosure.model` を `claude-haiku-4-5` にすると大幅に安くなります。
タイトルのみで一次判定し、インパクトの大きい開示だけPDF本文を深掘りする2段構成で
コストを抑えています。

## テスト

```bash
pytest
```

## 免責

本ソフトウェアは教育目的のサンプル実装です。投資判断は自己責任で行ってください。
