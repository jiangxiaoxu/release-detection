# release-detection

通过 GitHub Actions 定时检测软件或插件的新版本,并使用 GitHub issue comment 触发通知邮件。

## 当前已接入的 target

- `openai.chatgpt`
  - Source: VS Code Marketplace
  - URL: https://marketplace.visualstudio.com/items?itemName=openai.chatgpt
  - Channel policy: track both `stable` and `prerelease`
  - Current API result: only `prerelease` is exposed at the moment

## 工作方式

1. GitHub Actions 按 `.github/workflows/release-detection.yml` 的 cron 定时执行.
2. `scripts/release_detection.py` 读取 `targets.json`.
3. 脚本请求上游 release 源,拿到当前最新版本.
4. 每个 target 维护一个 tracking issue.
5. 当检测到版本变化时:
   - 更新 tracking issue body 中的隐藏状态
   - 追加一条新 comment
6. 订阅仓库或订阅该 issue 的用户会收到 GitHub 通知邮件.

## 初始化

1. 推送仓库到 GitHub.
2. 确保仓库启用了 GitHub Actions.
3. 手动运行一次 `Release Detection` workflow.
4. 在自动创建的 tracking issue 上点击 `Subscribe`.

## 新增 target

在 `targets.json` 中新增配置项。目前支持:

- `vs_code_marketplace`

示例:

```json
{
  "id": "example-extension",
  "name": "Example Extension",
  "source": {
    "type": "vs_code_marketplace",
    "publisher": "publisher-name",
    "extension": "extension-name",
    "itemUrl": "https://marketplace.visualstudio.com/items?itemName=publisher-name.extension-name",
    "includeStable": true,
    "includePrerelease": true
  },
  "notify": {
    "issueTitle": "[Release Detection] publisher-name.extension-name",
    "labels": ["release-detection", "automated"]
  }
}
```

## 本地验证

```bash
python scripts/release_detection.py --dry-run
```
