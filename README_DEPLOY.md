# Nova Chain 部署说明

## GitHub Pages
1. 进入 GitHub 仓库设置。
2. 打开 Pages，选择 Deploy from branch。
3. 选择主分支 `master`，根目录 `/`。
4. 确保仓库根目录包含 `.nojekyll` 文件，以避免静态页面被 Jekyll 处理。
5. 访问 `https://<username>.github.io/<repo>/`。

## Vercel
1. 导入当前仓库到 Vercel。
2. 选择默认设置即可。
3. 部署后访问生成的项目地址。

当前页面入口为：
- `index.html`：产品落地页，展示路线图、团队介绍与 CTA
- `nova.html`：交互式体验页，支持钱包、质押、合约与验证
