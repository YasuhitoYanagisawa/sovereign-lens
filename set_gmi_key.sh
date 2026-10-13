#!/usr/bin/env bash
# GMI Cloud の APIキーを Secret Manager に保存し、使えるモデル一覧を表示する（キーは画面・ログに出さない）
set -euo pipefail
read -rsp "GMI Cloud APIキーを貼り付けて Enter（表示されません）> " K; echo
if gcloud secrets describe gmi-api-key >/dev/null 2>&1; then
  printf '%s' "$K" | gcloud secrets versions add gmi-api-key --data-file=- >/dev/null
else
  printf '%s' "$K" | gcloud secrets create gmi-api-key --data-file=- --replication-policy=automatic >/dev/null
fi
echo "✅ Secret Manager に保存しました (gmi-api-key)"
echo "== 利用可能なモデル（chat系の一部） =="
curl -s -H "Authorization: Bearer $K" https://api.gmi-serving.com/v1/models \
  | python3 -c "import json,sys; d=json.load(sys.stdin); ids=[m.get('id') for m in d.get('data',[])]; print('\n'.join(ids[:40]) or d)"
