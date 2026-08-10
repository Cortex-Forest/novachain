# Nova Chain 网站部署说明

网站文件位于仓库子目录 `nova website/`：
- `index.html`：产品落地页，展示路线图、团队介绍与 CTA
- `nova.html`：交互式体验页，支持钱包、质押、合约与验证
- `404.html` / `.nojekyll` / `vercel.json`：静态站点配套文件

## Vercel（推荐）
1. 导入仓库到 Vercel。
2. Framework Preset 选 `Other`，Root Directory 填 `nova website`。
3. 部署后访问生成的项目地址。

## GitHub Pages
GitHub Pages 的 “Deploy from branch” 仅支持仓库根目录 `/` 或 `/docs`，无法直接发布 `nova website` 子目录。
- 推荐：配置 GitHub Actions 工作流，用 `actions/upload-pages-artifact` 上传 `nova website` 目录，再以 `actions/deploy-pages` 发布（Pages 源选择 `GitHub Actions`）。
- 备选：把网站文件移到 `/docs` 后使用 “Deploy from branch /docs”。
