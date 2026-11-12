# OCR Dock

- 单容器服务；部署文件放在 deploy/，数据目录为 /app/data，NAS 示例使用相对路径。
- OCR 使用本地 CPU 推理，默认端口 8083；API 不限制为五位数字。
- 提交前运行 python -m unittest discover -s tests -v；真实模型测试不可跳过。
- 后续提交更新 VERSION 与 CHANGELOG.md，正式发布前保持 0.x。
- 不提交实际 API Key、.env、data/、上传图片或运行日志。
- Docker 镜像为 sunqz/ocr-dock，通过 GitHub Actions 发布 amd64 / arm64。
