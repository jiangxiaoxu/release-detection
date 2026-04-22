# release-detection

通过 GitHub Actions 定时检测软件或插件的新版本,并使用 GitHub issue comment 触发通知邮件。

## 当前已接入的 target

- `openai.chatgpt`
  - Source: VS Code Marketplace
  - URL: https://marketplace.visualstudio.com/items?itemName=openai.chatgpt
  - Channel policy: track both `stable` and `prerelease`
  - Current API result: only `prerelease` is exposed at the moment
- `Codex`
  - Source: Microsoft Store Web
  - URL: https://apps.microsoft.com/detail/9plm9xgg6vks?hl=en-US&gl=US
  - Version signal: DisplayCatalog `PackageFullName` package version
  - Fallback signal: `packageLastUpdateDateUtc`
  - Current detected version: `26.421.620.0`
  - Publish: when `stable` changes, resolve the temporary MSIX link via rg-adguard, verify the Microsoft CDN download, and upload the MSIX to `msstore-codex-v<version>`
  - Retention: delete Microsoft Store Codex releases and tags older than 30 days during CI
- `Codex CLI`
  - Source: GitHub Releases
  - URL: https://github.com/openai/codex/releases
  - Channel policy: track only published full releases
  - Version signal: `tag_name`

## 工作方式

1. GitHub Actions 按 `.github/workflows/release-detection.yml` 的 cron 定时执行.
2. `scripts/release_detection.py` 读取 `targets.json`.
3. 脚本请求上游 release 源,拿到当前最新版本.
4. 每个 target 维护一个 tracking issue.
5. 当检测到版本变化时:
   - 更新 tracking issue body 中的隐藏状态
   - 追加一条新 comment
   - 对启用 release publish 的 Microsoft Store target,下载并校验 MSIX,然后上传到按版本创建的 GitHub Release
6. Microsoft Store 的下载链接只作为临时解析来源,长期可用的包保存在本仓库 GitHub Release asset.
7. CI 会删除超过保留期的 Microsoft Store Codex release 和 tag,默认只保留最近 30 天.
8. 订阅仓库或订阅该 issue 的用户会收到 GitHub 通知邮件.

## 初始化

1. 推送仓库到 GitHub.
2. 确保仓库启用了 GitHub Actions.
3. 手动运行一次 `Release Detection` workflow.
4. 在自动创建的 tracking issue 上点击 `Subscribe`.

## 新增 target

在 `targets.json` 中新增配置项。目前支持:

- `vs_code_marketplace`
- `microsoft_store_web`
- `github_releases`

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

```json
{
  "id": "example-msstore-app",
  "name": "Example App",
  "source": {
    "type": "microsoft_store_web",
    "productUrl": "https://apps.microsoft.com/detail/<product-id>?hl=en-US&gl=US",
    "productId": "<product-id>"
  },
  "notify": {
    "issueTitle": "[Release Detection] Microsoft Store Example App",
    "labels": ["release-detection", "automated"]
  },
  "release": {
    "enabled": true,
    "channel": "stable",
    "tagPrefix": "msstore-codex-v",
    "nameTemplate": "Microsoft Store Codex {version}",
    "retentionDays": 30
  }
}
```

```json
{
  "id": "example-github-release",
  "name": "Example CLI",
  "source": {
    "type": "github_releases",
    "owner": "owner-name",
    "repo": "repo-name",
    "releasesUrl": "https://github.com/owner-name/repo-name/releases"
  },
  "notify": {
    "issueTitle": "[Release Detection] owner-name/repo-name releases",
    "labels": ["release-detection", "automated"]
  }
}
```

## 本地验证

```bash
python scripts/release_detection.py --dry-run
```
