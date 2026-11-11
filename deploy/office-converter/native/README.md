# 原生部署 Office 转 PDF

本包面向麒麟 V10 Linux x86_64，使用系统上的 LibreOffice Writer 和 Calc。打印后台和 WorkBuddy 0.2.0 通过 HTTPS 调用转换接口。

## 1. 准备系统软件

通过与目标系统匹配的内网源或离线 RPM 安装 Python 3.12（含 pip 和 venv）、LibreOffice Writer 和 Calc、中文字体（例如 Noto CJK）、Nginx 和 `httpd-tools`（提供 `htpasswd`）。目标机已有 Fontconfig。Nginx 已提供内网 HTTPS 时可复用。Python 可并行安装，项目直接调用 `python3.12`。

```sh
python3.12 --version
command -v soffice
soffice --version
fc-list :lang=zh family | head
```

LibreOffice 官方 RPM 如果装在 `/opt`，用对应安装目录下的 `program/soffice` 进行检查。后续把同一个绝对路径填入 `/etc/print-hub-office/office.env` 的 `OFFICE_SOFFICE`。

在解压目录先验证原生转换。保留生成的两份 PDF，检查中文和分页。

```sh
work=$(mktemp -d)
mkdir "$work/word" "$work/excel"
soffice "-env:UserInstallation=file://$work/profile" --headless --convert-to pdf --outdir "$work/word" templates/office-check.docx
soffice "-env:UserInstallation=file://$work/profile" --headless --convert-to pdf --outdir "$work/excel" templates/office-check.xlsx
ls -lh "$work"/*/*.pdf
```

## 2. 启动转换服务

下面的离线安装命令适用于部署包。通过 Git 获取源码时，可使用 `native/requirements.txt` 准备 Python 依赖，并为目标系统准备离线 wheel。

首次部署时，将本包解压到 `/opt/print-hub-office`。以下命令以 `root` 执行。Nginx 运行账号及组应为 `nginx`。使用其他组时，同步修改 `print-hub-office.socket` 中的 `SocketGroup`。

```sh
cd /opt/print-hub-office
python3.12 -m venv .venv
.venv/bin/python -m pip install --no-index --find-links=wheelhouse -r requirements-native.txt
id -u print-hub-office >/dev/null 2>&1 || useradd --system --user-group --no-create-home --shell /sbin/nologin print-hub-office
install -d -m 0755 /etc/print-hub-office
install -m 0644 print-hub-office.socket print-hub-office.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now print-hub-office.socket
```

如果 `soffice` 在其他位置，先在 `/etc/print-hub-office/office.env` 写入 `OFFICE_SOFFICE=/实际路径/program/soffice`，再启动服务。

首次设置凭据时执行以下命令。已有密码文件时使用 `htpasswd -B` 更新账号。

```sh
htpasswd -cB /etc/nginx/print-hub-office.htpasswd print-hub
chown root:nginx /etc/nginx/print-hub-office.htpasswd
chmod 0640 /etc/nginx/print-hub-office.htpasswd
```

新建 HTTPS 入口时，将 `nginx.conf.example` 复制到 `/etc/nginx/conf.d/print-hub-office.conf`，替换监听 IP、内网域名和证书路径。已有 HTTPS 虚拟主机时，在 `http` 级别添加 `log_format`，并将示例中的 `location` 加入该虚拟主机。先执行 `nginx -t`，成功后执行 `systemctl enable --now nginx` 或重载现有 Nginx。将入口限制在公司内网或 VPN。如果 SELinux 拒绝访问套接字，按主机策略为 `/run/print-hub-office.sock` 设置 Nginx 可访问的标签并复测。

## 3. 验收

在新版技能的 `data/office-converter.json` 中，将 `url` 设为 `https://内网域名/office-converter`，填入 `print-hub` 的凭据。内网 CA 证书可通过 `ca_file` 提供给技能。打开打印后台点击「重新检测」，确认 Word、Excel 都显示通过。检查只发送内置样例。

如果麒麟主机还要运行打印后台，另行解压 WorkBuddy 0.2.0 项目包，在该目录创建 Python 3.12 虚拟环境。用本包的 `wheelhouse` 安装项目的 `requirements.txt`，配置同一个 HTTPS 地址，再运行 `.venv/bin/python scripts/bootstrap.py --serve`。打印后台需要能访问打印机的 IPP 地址。

排查转换服务可运行 `systemctl status print-hub-office.socket print-hub-office.service` 和 `journalctl -u print-hub-office.service -n 40`。停用本服务时，先移除本次增加的 HTTPS 路由，再停止两个单元。
