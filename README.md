# ticket-archiver

12306 铁路电子发票自动归档。计算在**腾讯云云函数 SCF**（`ap-beijing`，Python 3.10），
存储仍在**阿里云**（OSS 放文件、Tablestore 放元数据）。

每天 20:00（北京时间）登录 QQ 邮箱，抓 `12306@rails.com.cn` 的发票邮件，
解析发票要素，按**发票号码**幂等归档：文件写 OSS，元数据写 Tablestore，
有新发票时推送到飞书群。

## 处理链路

```
QQ 邮箱 IMAP
   └─ 附件 .zip / .ofd / .pdf
        ├─ OFD：解包读 rai_issuer_*.xml（XBRL，字段最全）
        ├─ PDF：pypdf 抽文本 + 正则
        └─ ZIP：内含同一张发票的 pdf + ofd，两个都取
   ↓ 发票号码（20 位）去重
阿里云 OSS  invoices/<日期> <出发>-<到达> <车次>/<发票号>/<发票号>.pdf|.ofd
Tablestore  ticket-archiver / invoices   ← 元数据，主键 invoice_no
   ↓ 有新增
飞书群「12306 发票归档」
```

## 目录结构

```
invoices/<乘车日期> <出发>-<到达> <车次>/<发票号>/<发票号>.pdf|.ofd
```

一趟车一个文件夹，每张票一个子文件夹，里面同时有 ofd 与 pdf。

## 去重

同一张发票会以多种形态出现（zip 内含 pdf+ofd、裸 ofd、裸 pdf，甚至同一封邮件重复），
因此**不按文件名去重**，而是：

1. 解析出发票号码 `ElectronicInvoiceRailwayETicketNumber`；
2. 以 `invoice_no` 为主键查 Tablestore，命中即跳过；
3. 同一批次内再用内存集合去重。

重复执行是安全的：已验证重跑 `uploaded=0`。

## 通知卡片

飞书用 `interactive` 卡片（schema 2.0），不用纯文本：

- 标题栏：`🚄 12306 发票归档 · 月-日 时:分`
- 副标题：本次 N 张 · 累计 M 张（累计由 `invoices` 主键范围扫描统计）
- 正文：**竖排**，每张发票两行 —— 第一行加粗「乘车日期　出发 → 到达」，第二行「车次　类型　金额」
- 超过 10 张只展示前 10 张并注明剩余数量
- 底部：发送时间；不暴露存储桶名
- 卡片被拒时自动回退纯文本，保证通知不丢

群头像是高铁 + `12306` 的方形图（`assets/avatar.png`）。

## 数据模型（`invoices`）

| 字段 | 来源 |
|---|---|
| `invoice_no`（主键） | 发票号码 |
| `issue_date` / `business_type` | 开票日期 / 购·退 |
| `travel_date` / `depart_time` | 乘车日期 / 开车时间 |
| `from_station` / `to_station` / `train_no` | 行程 |
| `seat_level` / `carriage` / `seat` | 席别、车厢、座位 |
| `fare` / `tax_rate` / `tax_amount` / `amount_excl_tax` | 金额 |
| `eticket_no` / `passenger_name` / `id_masked` | 电子客票号、乘客（证件号打码） |
| `oss_pdf_key` / `oss_ofd_key` / `formats` | 归档位置 |
| `source_file` / `archived_at` | 来源附件、归档时间 |

## 部署

```bash
python3 scripts/deploy.py
```

依赖：

- `tccli` 已配置（`ap-beijing`）；
- Infisical `tools/prod` → `/ticket-archiver`：`EMAIL_ACCOUNT`、`EMAIL_AUTH_CODE`、
  `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_CHAT_ID`，以及阿里云 RAM 用户
  `ticket-archiver-scf` 的 `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET`（OSS + Tablestore 最小权限）。

`scripts/deploy.py` 会构建纯 Python 代码包（约 2 MiB）、上传到 COS 中转，
再创建/更新 SCF 函数与每日定时触发器（`0 0 20 * * * *`，北京时间）。

构建细节见 `scripts/build_pkg.py`：去掉平台二进制、用纯 Python CRC32C、
aiohttp 改可选、剔除 numpy。因此代码包与运行平台无关，本地可直接测试。

## 定时执行（GitHub Actions）

[`.github/workflows/archive.yml`](.github/workflows/archive.yml) 每天
12:00 UTC（20:00 北京时间）直接跑 `python3 fc/index.py`，也可手动触发。
每次运行都 checkout `master` 最新代码，改完推送即生效，不需要额外部署。

仓库 Secrets：`EMAIL_ACCOUNT`、`EMAIL_AUTH_CODE`、`FEISHU_APP_ID`、
`FEISHU_APP_SECRET`、`FEISHU_CHAT_ID`、`ALIBABA_CLOUD_ACCESS_KEY_ID`、
`ALIBABA_CLOUD_ACCESS_KEY_SECRET`（阿里云 RAM 用户 `ticket-archiver-scf`）。

归档执行只跑 GitHub Actions；腾讯云 SCF、CAM 部署身份与阿里云函数计算均已删除。

## 本地验证

```bash
# 用环境变量跑一次（需要 OSS / Tablestore 权限）
EMAIL_ACCOUNT=... EMAIL_AUTH_CODE=... \
ALIBABA_CLOUD_ACCESS_KEY_ID=... ALIBABA_CLOUD_ACCESS_KEY_SECRET=... \
python3 fc/index.py
```

## 运维

- 日志：函数计算控制台 → `ticket-archiver` → 调用日志；
- 手动触发：`aliyun fc invoke-function --function-name ticket-archiver --biz-body '{}'`；
- 重建归档：删除对应 `invoices` 行后再次触发，会重新上传并重新入库；
- 令牌轮换：QQ 授权码变更后更新 Infisical，再执行一次 `scripts/deploy.py`。

## 说明

- 仅归档发票，不登录 12306、不购票；
- 邮箱授权码只存在于 Infisical 与函数环境变量，仓库中没有任何真实凭据；
- 存储桶名只记录在私有 infra 资源清单，仓库与通知中均不出现；
- 早期基于 `bdpan` 的本地 CLI 版本已被云函数版取代，见 Git 历史。