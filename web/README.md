# TesterAgent frontend (WP-F0 / F1)

## Scripts

- `npm run dev` — Vite dev server (proxies `/api` → `http://127.0.0.1:8080`)
- `npm run build` — production bundle → `dist/`
- `npm test` — Vitest (MSW)

## Dev

MSW is enabled by default in dev; set `VITE_ENABLE_MSW=0` to hit the real backend.

- F0 验收台：`/_dev/f0`
- F1 ChatPage：`/`（需求输入 → 建任务/run → SSE；澄清卡；`@链路`/`@测试点` change_request）
