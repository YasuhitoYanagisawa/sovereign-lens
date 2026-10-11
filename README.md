# Sovereign Lens

世界の政府系ファンドが選んだ日本株を、あなたの資産サイズで。

サウジアラビアのPIF、シンガポールのGIC、ノルウェーのNBIMなど、政府系ファンド（SWF）が保有する日本株を集めます。エージェントが保有比率・推定取得単価・保有期間・購入理由を調べ、利用者の金額・リスク許容度・投資期間に合わせた配分案（株数と買い方まで）を作ります。

第5回 Agentic AI Hackathon with Google Cloud への応募作品です。

![architecture](docs/architecture.png)

## 主な機能

| 機能 | 内容 |
|---|---|
| SWF保有の収集 | EDINET API v2 の大量保有報告書・変更報告書（docType 350/360）と、NBIM公開APIの全保有銘柄から集めます。出典URL付きの検証済みデータ（43件）も同梱しています。 |
| 推定取得単価 | 報告書の「取得資金合計 ÷ 保有株数」を、株式分割で調整して算出します。 |
| 保有期間 | 初回報告日と、NBIMの1998・2005・2010・2015・2020年末の保有一覧から推定します。 |
| Sovereign Conviction Score | 保有比率30点、複数ファンドの重なり20点、保有期間20点、直近の売買15点、現在値と取得コストの比較15点の合計です。 |
| 購入理由の調査 | Gemini と Google検索グラウンディングを使い、事実と推測を分けて出典付きで要約します。 |
| 戦略の作成 | コア（TOPIX）、SWF追随の個別株、現金に分けます。単元株か単元未満株かを判定し、分割購入の計画も付けます。 |
| 独立審査 | 別のエージェントが集中リスクや断定的な表現をチェックします。指摘があれば、オーケストレーターが自分で作り直します。 |

## エージェント設計

- `sovereign_lens`（オーケストレーター、ADK `LlmAgent`）：収集→採点→理由調査→配分→審査の手順を、状況を見ながら自分で進めます。
- `rationale_researcher`：`google_search` ツールを持つサブエージェントです。`AgentTool` として呼び出します。
- `plan_reviewer`：独立した審査エージェントです。プランの数値は変更できません。
- 数値はすべて決定論的なツール（`build_investment_strategy`）が計算します。LLMが数字を作ることはありません。

## ガバナンス・セキュリティ・可観測性

- **ADK Plugin（`GovernancePlugin`）**
  - プロンプトインジェクションと、発注・認証情報の要求を検知してブロックします。
  - ツールは許可リストにあるものだけ呼べます。1回の実行で呼べるのは30回までです。
- **ガードレール**：1銘柄・1業種の上限、現金比率の下限、リスク別の銘柄規模フィルタはコードで強制します。テストは42件です。
- **発注機能なし**：プランは人が確認・承認する前提です（CSVの発注メモを出力します）。
- **コスト管理**：IPごとのレート制限、Cloud Run の最大インスタンス数3、最小0（使わないときは課金されません）。
- **可観測性**：Cloud Trace（OpenTelemetry）、Cloud Logging の構造化監査ログ（`jsonPayload.audit=true`）、UIでのリアルタイムトレース表示。
- **最小権限**：EDINETキーは Secret Manager に保管し、実行用サービスアカウントの権限は最小限にしています。

## デプロイ（Google Cloud Shell）

```bash
git clone https://github.com/yasuhitoyanagisawa/sovereign-lens.git
cd sovereign-lens
chmod +x deploy.sh
./deploy.sh <PROJECT_ID> [EDINET_API_KEY]
```

## ローカル実行

```bash
pip install -r requirements.txt pytest
pytest                       # LLMなしで42件のテストが通ります
export GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=<id> GOOGLE_CLOUD_LOCATION=global
uvicorn app.main:app --reload --port 8080
```

## 免責事項

本ソフトウェアは公開情報に基づく情報提供を目的としたもので、投資助言・勧誘ではありません。推定取得単価は報告書の取得資金から計算した推定値です。データの正確性は保証しません。
