# AGENTS.md instructions for G:\Project\release-detection

## Microsoft Store Codex download parsing

- When the user asks to download the Microsoft Store Codex package, product id is `9PLM9XGG6VKS`.
- Always use rg-adguard to resolve Microsoft Store package links. Treat rg-adguard as a link index, not as the file source.
- rg-adguard request shape: `type=ProductId&url=9PLM9XGG6VKS&ring=Retail&lang=en-US` posted to `https://store.rg-adguard.net/api/GetFiles`.
- Select the `.msix` row whose filename matches the target package version, for example `OpenAI.Codex_26.421.620.0_x64__2p2nqsd0c76g0.msix`.
- Download only from the returned Microsoft CDN URL, normally under `*.dl.delivery.mp.microsoft.com`.
- Save packages under `.\downloads\`; `downloads/` is ignored and must not be committed.
- CI publishes verified Microsoft Store Codex MSIX files to versioned GitHub Releases using tag format `msstore-codex-v<version>`.
- CI retention cleanup deletes Microsoft Store Codex GitHub Releases and their matching tags when they are older than `release.retentionDays`; default/current value is `30`.
- After downloading, always verify file identity before telling the user it is ready:
  - `Get-FileHash <path> -Algorithm SHA1` must match the SHA-1 reported by the link index.
  - `Get-AuthenticodeSignature <path>` should report `Status: Valid`.
  - File name must match the expected package full name and version.
- Report the exact commands, local file path, byte size, SHA-1, signature status, and whether the file came from Microsoft CDN.
