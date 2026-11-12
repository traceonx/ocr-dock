# OCR Dock

基于 [ppllocr 2.2](https://github.com/gitpetyr/ppllocr) 的独立验证码识别 API 服务。通过 HTTP 上传图片，获取文字、每个字符的置信度及坐标，适合签到程序、脚本和其他应用调用。

## 功能

- 支持 Base64、图片 Data URL、文件表单和图片二进制输入。
- 支持数字、大小写字母及上游模型支持的符号，不限制为五位数字。
- CPU 本地推理，模型随镜像提供，运行时无需联网下载模型或调用第三方识别服务。
- 模型启动时加载并复用，支持置信度与重叠阈值调整。
- API Key 鉴权、图片大小与像素限制、限流和有界并发队列。
- 健康检查与交互式 API 文档。
- 单容器、NAS 用户权限与相对路径数据目录。
- GitHub Actions 自动测试并发布 amd64 / arm64 多架构镜像。

## Docker 部署

将以下内容保存为 `compose.yaml`，在镜像发布成功后启动：

```yaml
name: ocr-dock
services:
  app:
    image: sunqz/ocr-dock:latest
    container_name: ocr-dock
    environment:
      TZ: Asia/Shanghai
      PUID: "1000"
      PGID: "1000"
      API_KEY: ""
    ports:
      - "8083:8080"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

```sh
docker compose pull
docker compose up -d
```

完整 NAS 配置见 [deploy/nas/compose.yaml](deploy/nas/compose.yaml)。执行 `id 用户名` 查询 UID/GID 后填写，群晖常见值是 `1026` / `100`。端口可改成自己的空闲端口。

- API 文档：`http://NAS地址:8083/docs`
- 健康检查：`http://NAS地址:8083/health`，模型就绪返回 HTTP 200；加载失败返回 503。

### API Key

默认自动生成随机密钥并保存到 `./data/api_key`，重启后继续使用：

```sh
sudo cat ./data/api_key
```

也可设置 `API_KEY`，自定义密钥至少 16 个字符。环境变量优先于文件，不会覆盖已有文件。跨公网访问时使用 HTTPS 反向代理，调用端通过请求头发送密钥。

数据目录只保存生成的 API Key，上传图片不写入该目录。保留数据目录即可保留自动生成的密钥。

## API 调用

识别接口为 `POST /ocr`，支持 `X-API-Key` 或 `Authorization: Bearer` 鉴权，任选一种即可。

### 图片文件

```sh
export OCR_API_KEY='替换为你的密钥'
curl -sS http://127.0.0.1:8083/ocr \
  -H "X-API-Key: $OCR_API_KEY" \
  -F 'file=@captcha.png'
```

表单可加入 `conf` 和 `iou`，如 `-F 'conf=0.6'`。

### Base64 / JSON

```json
{
  "image_base64": "图片Base64内容，也支持data:image/png;base64,...",
  "conf": 0.25,
  "iou": 0.45
}
```

Python 示例：

```python
import base64
import os
import requests

with open('captcha.png', 'rb') as image:
    encoded = base64.b64encode(image.read()).decode()

response = requests.post(
    'http://NAS地址:8083/ocr',
    headers={'X-API-Key': os.environ['OCR_API_KEY']},
    json={'image_base64': encoded},
    timeout=30,
)
response.raise_for_status()
print(response.json()['text'])
```

无需第三方依赖的客户端见 [examples/client.py](examples/client.py)。

### 图片二进制

```sh
curl -sS http://127.0.0.1:8083/ocr \
  -H "Authorization: Bearer $OCR_API_KEY" \
  -H 'Content-Type: image/png' \
  --data-binary @captcha.png
```

### 返回结构

```json
{
  "ok": true,
  "text": "k7Mz",
  "details": [
    {"char": "k", "conf": 0.92, "box": [20.0, 17.3, 52.9, 67.7]}
  ],
  "elapsed_ms": 120.5,
  "request_id": "请求标识"
}
```

以上为结构示例，`details` 实际包含每个检测字符，坐标对应原图像素。耗时包含输入解析、排队与识别，不是固定性能指标。未识别到字符时仍返回 `ok: true`、`text: ""`，由调用端决定换图或转人工。请结合长度、字符集及置信度判断结果。

错误返回：

```json
{
  "ok": false,
  "error": {"code": "unauthorized", "message": "API Key 无效或缺失"},
  "request_id": "请求标识"
}
```

| HTTP 状态 | 含义 |
| --- | --- |
| 400 / 422 | JSON、Base64、图片或参数无效 |
| 401 | API Key 无效或缺失 |
| 413 | 请求、图片大小或像素超限 |
| 415 | 图片或请求格式不支持 |
| 429 | 限流、队列已满或排队超时，按 `Retry-After` 重试 |
| 503 | 模型未就绪，查看健康检查及容器日志 |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_KEY` | 自动生成 | 至少 16 位，留空时使用 `/app/data/api_key` |
| `PUID` / `PGID` | `1000` / `1000` | 容器运行用户及用户组 |
| `MAX_IMAGE_BYTES` | `2097152` | 单张图片最大 2 MiB |
| `MAX_PIXELS` | `4000000` | 最大像素数量：宽 × 高 |
| `MAX_CONCURRENCY` | `1` | 同时识别数，范围 1–4 |
| `MAX_QUEUE` | `4` | 额外等待任务数 |
| `QUEUE_WAIT_SECONDS` | `10` | 排队等待超时秒数 |
| `RATE_LIMIT_PER_MINUTE` | `60` | 每个连接来源 IP 的滑动窗口限流，`0` 关闭 |
| `ORT_THREADS` | `2` | ONNX CPU 推理线程数 |
| `CORS_ORIGINS` | 空 | 可选的浏览器来源列表，逗号分隔，不支持 `*` |
| `DATA_DIR` | `/app/data` | 持久化数据目录 |

支持 PNG、JPEG、BMP 和静态 WebP。拒绝动画；请求体另有限制，覆盖 Base64 开销与表单元数据。限流按直接连接的 IP 计算，不信任转发头，反向代理后的客户端通常共享代理 IP 额度。

服务只运行一个 Uvicorn 进程，共享模型和队列。客户端断开后，已开始的识别仍占用名额直到结束。扩容时优先调整受控并发或部署多个实例。

## 镜像发布与更新

推送到 `main` 后，GitHub Actions 自动执行：

```text
API 与真实模型测试 → amd64 / arm64 原生构建 → 合并并发布多架构镜像
```

镜像：`sunqz/ocr-dock`；标签：`latest`、版本号、`sha-xxxxxxx`。支持 `v*` 标签和手动运行。Pull request 只测试和构建，不发布。

在本仓库 Settings → Secrets and variables → Actions 中配置：

- `DOCKER_USERNAME`：有权推送 `sunqz/ocr-dock` 的 Docker Hub 用户名。
- `DOCKER_PASSWORD`：具有写入权限的 Docker Hub Access Token。

仓库密钥不会从其他项目自动继承。更新时执行 `docker compose pull && docker compose up -d`，也可固定具体镜像版本。

## 从源码运行

```sh
git clone https://github.com/traceonx/ocr-dock.git
cd ocr-dock
cp .env.example .env
docker compose -f compose.yaml -f deploy/compose.build.yaml up -d --build
```

Python 3.12 开发与验证：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
uvicorn ocr_dock.app:app --host 127.0.0.1 --port 8083 --workers 1 --no-proxy-headers
```

本地 Python 默认使用 `./data`。模型随 `ppllocr==2.2` 安装；真实识别测试使用独立生成的已知答案图片。

## 上游项目

OCR 能力来自 [ppllocr](https://github.com/gitpetyr/ppllocr)，使用其 2.2 版模型、预处理及后处理；OCR Dock 提供 HTTP 服务与部署封装。上游 MIT 许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
