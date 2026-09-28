import { expect, test } from '@playwright/test'

/**
 * 轻量 UI smoke：导航壳 + 会话引导 + 设置页（MSW）。
 * 主路径/回退/崩溃 E2E 在 server/tests/test_e2e_main.py（ASGI）。
 */
test.describe('WP-X1 UI smoke (MSW)', () => {
  test('壳导航与会话页可进入', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByText('TesterAgent')).toBeVisible()
    await expect(page.getByRole('link', { name: '会话' })).toBeVisible()
    await expect(page.getByRole('link', { name: '工作区' })).toBeVisible()
    await expect(page.getByRole('link', { name: '设置' })).toBeVisible()

    await expect(
      page.getByRole('heading', { name: '需求文档（Markdown）' }),
    ).toBeVisible({ timeout: 15_000 })
  })

  test('设置页加载模型表单', async ({ page }) => {
    await page.goto('/settings')
    await expect(page.getByTestId('settings-page')).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByLabel('模型')).toBeVisible()
    await expect(page.getByTestId('model-save-btn')).toBeVisible()
  })

  test('工作区页可打开', async ({ page }) => {
    await page.goto('/workspaces')
    await expect(page.getByTestId('workspaces-page')).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByRole('heading', { name: '工作区', exact: true })).toBeVisible()
  })
})
