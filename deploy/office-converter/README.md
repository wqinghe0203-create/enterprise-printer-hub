# 在内网部署 Office 转 PDF

需要 Linux、Docker Engine 28 或以上、Docker Compose v2，以及同机运行的内网 HTTPS 反向代理。将本目录的 `compose.yaml`、`ingress.conf` 和反代示例复制到测试服务器。确认内网域名与证书匹配，入口只允许公司内网或 VPN 访问。

## 启动转换服务

在部署目录创建仅管理员可读的 `.env`。以下命令适用于首次部署。运行前确认目录中没有现有 `.env`。

```sh
umask 077
(set -C; printf 'OFFICE_CONVERTER_PASSWORD=%s\n' "$(openssl rand -hex 32)" > .env)
docker compose config --quiet
docker compose up -d
docker compose ps
```

Compose 在 `127.0.0.1:13000` 提供 HTTP 上游。端口冲突时，在 `.env` 增加 `OFFICE_CONVERTER_PORT=其他端口`，并同步调整反代配置。转换容器只连接隔离网络，Office 文件和转换产物只写入容器的临时文件系统。容器标准日志不保留，代理访问日志记录 HTTP 状态和耗时。

若现有入口为 Nginx，参考 `nginx.conf.example`，在 HTTPS 虚拟主机中添加精确的转换路由。其他代理使用相同路径和上游，透传 `Authorization` 与 multipart 请求，允许请求体最多 `26214656` 字节，转换等待至少 100 秒，关闭请求和响应的磁盘缓冲。验证配置后重新加载现有代理。若入口与容器分属不同主机，需要先建立受控的私有上游连接。

## 配置技能并联调

在运行 WorkBuddy 或打印后台的电脑上，按项目根目录 README 的「配置 Office 转换服务」创建配置。`url` 指向反代前缀，例如 `https://convert.intra.example/office-converter`。用户名是 `print-hub`，密码由管理员从服务器 `.env` 安全分发。

在技能目录运行 `bootstrap.py --retry-office`，或打开本机网页控制台点击「重新检测」。分别确认 `word` 和 `excel` 通过。测试使用技能自带的合成文档。CLI 检查启动后用 `--office-status` 读取后台检查进度。代理入口配置了自定义前缀时，使用对应的 `url`。

排查时，先用 `docker compose ps` 确认两个服务正在运行，再核对代理返回的 HTTP 状态。轮换密码后运行 `docker compose up -d`，并更新打印后台和 CLI 使用的配置。停止测试部署可运行 `docker compose down`，并移除本次增加的 HTTPS 路由。
