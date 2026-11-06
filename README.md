# 微信双开 · iOS 自签完整流程

> 在 **不越狱**、**不使用任何第三方账号/证书** 的前提下，于同一台 iPhone 上运行两个微信，
> 并把「7 天续签」从"每次插电脑"变成"手机上点一下"。
>
> 本文所有步骤均在 **iPhone 16 / iOS 27.3** 上实测跑通。

---

## 一、原理

| 事实 | 含义 |
|---|---|
| iOS 以 `CFBundleIdentifier` 作为 App 唯一标识 | 原版 `com.tencent.xin` 已被占用，新包改成 `com.tencent.xin1` 即可共存 |
| App Store 下发的二进制是 FairPlay 加密的（`cryptid=1`） | 重签名后无法解密，直接用官方包自签必然闪退，**必须使用已砸壳（`cryptid=0`）的包** |
| 免费 Apple ID 签发的 provisioning profile 只有 7 天 | 到期 App 无法启动，需要重新签名（覆盖安装，数据保留） |
| 签名需要 Apple 的 Anisette 认证数据 | 电脑端由 Sideloadly/iloader 内置生成；手机端由 SideStore 的设备端 Anisette（依赖 LocalDevVPN）生成 |

所以整件事拆成两半：**改包**（一次性）和 **重签**（周期性）。

---

## 二、工具与下载地址

| 工具 | 用途 | 地址 |
|---|---|---|
| **iloader** | 一次性把 SideStore 装进手机 + 写入 pairing file | <https://github.com/nab138/iloader/releases> · <https://iloader.app> |
| **SideStore** | 手机端自签与 7 天续签 | <https://github.com/SideStore/SideStore/releases> · <https://sidestore.io> |
| **Sideloadly** | 首次安装用（内置 Anisette，最稳） | <https://sideloadly.io> |
| **LocalDevVPN** | SideStore 的设备端 Anisette 依赖它 | App Store 搜索，或 <https://apps.apple.com/app/localdevvpn/id6755608044> |
| **砸壳 IPA 源** | 未签名 decrypted IPA | <https://github.com/Taosky/unsigned-ipa-cn/releases> |

macOS 上 iloader 用 `iloader-darwin-universal.dmg`；SideStore 用 `SideStore.ipa`。

---

## 三、全流程

### 步骤 1 · 拿到砸壳 IPA

```bash
# 微信原版砸壳包（release tag: wechat，按版本号命名）
https://github.com/Taosky/unsigned-ipa-cn/releases/download/wechat/dumponly_8.0.31.ipa

# 更新的多开专用包（release tag: fchat，bundle id 已经是 com.tencent.fchat）
https://github.com/Taosky/unsigned-ipa-cn/releases/download/fchat/fchat_8.0.71.ipa
```

GitHub 直连国内很慢（实测 50–90 KB/s），用多线程 Range 下载器 + 镜像：

```bash
python3 tool/dl2.py "https://gh-proxy.com/https://github.com/Taosky/unsigned-ipa-cn/releases/download/fchat/fchat_8.0.71.ipa" fchat.ipa 24 5
```

**选包原则：版本不能太旧。** 微信服务端有最低版本门槛，8.0.31（2022-12）会被直接拦：

> 你的应用版本过低，请升级至最新版本后再登录。

实测 **8.0.71（2026-04 包）可正常登录**。

验证包是否真砸壳：

```bash
otool -l Payload/WeChat.app/WeChat | grep -A5 LC_ENCRYPTION_INFO_64
# cryptid 0 → 已解密，可用
# cryptid 1 → 仍是商店加密包，自签必崩
```

### 步骤 2 · 改 bundle id / 应用名

```bash
python3 tool/ipa_rebrand.py \
  -i fchat_8.0.71.ipa \
  -o WeChat.ipa \
  --bundle-id com.tencent.xin1 \
  --display-name WeChat \
  --name WeChat \
  --add-device iPhone17,3
```

脚本做的事：

1. 用系统 `unzip` 解包（保留 0755 权限与符号链接，Python `zipfile` 会丢）；
2. 主 bundle 的 `CFBundleIdentifier` → `--bundle-id`；
3. 所有嵌套 `.appex` / `Watch/*.app` 的 id **按旧主 id 前缀等比替换**
   （`com.tencent.xin.share` → `com.tencent.xin1.share`），iOS 强制要求 extension id 以主 app id 为前缀；
4. `CFBundleDisplayName` / `CFBundleName` → 新名，并同步 19 个 `*.lproj/InfoPlist.strings`
   （桌面显示名优先取本地化 strings，只改 Info.plist 不够）；
5. 删除 **声明了 `CFBundleExecutable` 但文件缺失** 的嵌套 bundle（砸壳残骸），否则安装报 `code -77`；
6. 删除 `UISupportedDevices` 白名单（iPad/Mac 渠道的包会带，iPhone 装会被 `DeviceNotSupportedByThinning` 拦）；
7. 剥离 `_CodeSignature` / `SC_Info` / `embedded.mobileprovision`，对每个 Mach-O 执行 `codesign --remove-signature`；
8. Mach-O 统一 `chmod 0755`；
9. `zip -qry` 重打包，排除 `.DS_Store`、`__MACOSX`。

常用参数：

```bash
--version 8.0.90 --build 8.0.90.20   # 版本号伪装，绕过"版本过低"（是否生效取决于客户端读 plist 还是二进制常量）
--keep-signature                     # 保留原签名（默认剥离）
--keep-broken-bundles                # 保留残缺 bundle（默认删除）
--keep-supported-devices             # 保留设备白名单（默认删除）
--workdir DIR                        # 指定解包目录
```

### 步骤 3 · 换图标（可选）

若源包图标带第三方水印（fchat 包的图标右下角有 `PRO` 角标），从 App Store 接口取官方 1024 图：

```bash
curl -s "https://itunes.apple.com/lookup?bundleId=com.tencent.xin&country=cn"   # 取 artworkUrl512，改 1024x1024bb.jpg
```

替换包内的 `icon.png`（并补齐 `icon@2x.png` 120px、`icon@3x.png` 180px）：

```
Payload/WeChat.app/icon.png       1024×1024
Payload/WeChat.app/icon@2x.png    120×120
Payload/WeChat.app/icon@3x.png    180×180
```

该包的 `Info.plist` 中 `CFBundleIconFiles` 已声明这三个名字，补齐后 3x 屏会用 180px 那张。
仓库内 `icon/wechat_official_1024.png` 即官方原图。

### 步骤 4 · 首次安装

**方式 A：Sideloadly（最稳，内置 Anisette）**

```
Sideloadly → 选 WeChat.ipa → 填自己的 Apple ID → 不勾 automatic bundle ID → Start
iPhone：设置 → 通用 → VPN与设备管理 → 信任开发者证书
iPhone：设置 → 隐私与安全性 → 开发者模式 → 打开并重启
```

> ⚠️ **`Use automatic bundle ID` 必须取消勾选。** 勾上会让 Sideloadly 重写 id，
> 你改的 `com.tencent.xin1` 白费，且后续每次续签 id 都会变 → 变成新 App、数据全丢、
> 微信服务端记一次"新客户端登录"。

**方式 B：iloader 的「导入 IPA」** —— 同样可用，且顺手处理 pairing file。

### 步骤 5 · 部署 SideStore（让续签脱离电脑）

**5.1 iPhone 装 LocalDevVPN 并 Connect**（SideStore 的设备端 Anisette 依赖它）

**5.2 Mac 上 iloader**

```
iloader → 登录 Apple ID → 选中设备 → Install SideStore (Stable)
或 → 导入 IPA → 选 SideStore.ipa
```

**5.3 推 pairing file（关键）**

iloader 左侧 → **管理配对文件（⌘P）** → 选 `SideStore` → 放置。

没有 pairing file 时 SideStore 所有操作都会报：

```
Operation Failed
Pairing Required: Without a valid pairing file, SideStore operations cannot connect to your device.
```

**5.4 iPhone 上收尾**

```
设置 → 通用 → VPN与设备管理 → 信任开发者
开启开发者模式（已开可跳过）
LocalDevVPN → Connect
打开 SideStore → 登录 Apple ID → My Apps → 点 SideStore 自己的 "7 DAYS" → 刷新
```

**5.5 把微信交给 SideStore 管**

```
把 WeChat.ipa 传到 iPhone → SideStore 左上 + → 导入 → 等待签装
```

bundle id 相同（`com.tencent.xin1`）→ 覆盖安装，**聊天记录与登录态保留**。
之后每 7 天在 My Apps 里点微信那行的天数即可，无需电脑。

---

## 四、踩坑记录

| 现象 | 根因 | 解决 |
|---|---|---|
| `bundle_main_missing ... WeChatWatchNative.app/WeChatWatchNative` (code -77) | 砸壳包残缺：`com.apple.WatchPlaceholder` 的 Info.plist 声明了 `CFBundleExecutable` 但 watchOS 二进制被删 | 脚本 `drop_broken_bundles()` 整包删除（副作用：Apple Watch 端微信消失） |
| `DeviceNotSupportedByThinning` | 主 plist 的 `UISupportedDevices` 只有 `iPad*` + `MacFamily20,1`（包源自 Mac/iPad 渠道） | 删除整个 `UISupportedDevices`，并校验 `UIDeviceFamily` 含 `1` |
| 安装后图标是空白 / 可执行文件无法运行 | Python `zipfile` 解包丢失 0755 权限 | 改用系统 `unzip` 解包 + 打包前 `chmod 0755` |
| `你的应用版本过低，请升级至最新版本后再登录` | 微信服务端最低版本门槛，8.0.31 已超期 | 换 8.0.7x 的包；或 `--version` 伪装 plist 版本号 |
| iloader `Failed to retrieve certificate identity` / `error sending request for url (developerservices2.apple.com)` | **macOS 系统代理指向了没在运行的 Clash 端口**，Rust `reqwest` 读系统代理 → 请求发往死端口 | 关闭系统代理：`networksetup -setwebproxystate "Wi-Fi" off`，HTTPS/SOCKS 同理，三个网络服务都要关 |
| SideStore 登录 `503 Service Temporarily Unavailable`（HTML 里 `<center>Apple</center>`） | Apple 端拒绝：Anisette 数据无效 / 出口 IP 被风控 / 版本 bug | 升级 SideStore（0.7.0 起 changelog 明写 `fixes for 503 during sign-in`）；确认 LocalDevVPN 已连接；换网络 |
| 打开 App 弹 `无法验证 App — 需要互联网连接以验证是否信任开发者` | iOS 需访问 `ocsp.apple.com` 校验证书，国内 DNS 常劫持到无效节点 | ① 手动信任证书 ② iPhone DNS 改 `223.5.5.5` / `119.29.29.29` ③ 换蜂窝数据 ④ 手机走可用代理 |
| iloader 下载 SideStore 极慢 | GitHub release asset 单线程、无断点续传 | 用 `tool/dl2.py` 多线程下载后走「导入 IPA」；或换 `gh-proxy.com` 镜像 |

---

## 五、关于封号

**改 bundle id、改显示名、换图标，都不会提高封号概率。** 这些都是本地 UI/元数据层面的改动，
微信不上报 `CFBundleDisplayName`，也不校验它。

真正决定风险的是：

| 维度 | 说明 |
|---|---|
| 是否 App Store 分发 | 自签包没有 `_MASReceipt/receipt`，微信可据此识别非商店客户端 |
| 签名主体 | 个人免费证书的 Team ID 与 Apple 官方分发签名不同 |
| 行为 | 同设备批量登录新号、群发营销、外挂插件、频繁切号、异地 IP —— 这些才是拉高风险的真正原因 |

降低风险的实操：

1. 主号留在 App Store 官方客户端，双开位只放小号/工作号；
2. **不要注入任何插件**（抢红包、自动回复、去广告一律别碰）；
3. 一旦出现"非官方客户端 / 登录环境异常"，立即停用该客户端，别反复重试；
4. 续签时 bundle id 与 Apple ID 都保持不变，避免触发"新客户端登录"记录。

---

## 六、仓库内容与下载

```
.
├── README.md                       本文
├── tool/
│   ├── ipa_rebrand.py              IPA 改包脚本（改 id / 改名 / 剔残包 / 解设备白名单 / 剥签名 / 修权限 / 重打包）
│   └── dl2.py                      GitHub Release 多线程 Range 下载器（镜像轮换 + 429 退避 + 续传）
└── icon/
    └── wechat_official_1024.png    微信官方 App 图标 1024×1024
```

### 成品 IPA

**去 [Releases](https://github.com/ZoharZhao/double-wechat/releases) 下载 `WeChat_clean.ipa`。**

成品不放仓库本体，因为单个 IPA 约 288 MB，超过 GitHub 单文件 100 MB 的硬限制；
Release asset 上限 2 GB，因此走 Release 分发。

| 项 | 值 |
|---|---|
| 文件名 | `WeChat_clean.ipa` |
| bundle id | `com.tencent.xin1` |
| 显示名 | `WeChat` |
| 版本 | 8.0.71 / 8.0.71.34 |
| 大小 | 288 MB |
| sha256 | `7133d7a1495d88bbeba8ed5b652e866dd946f3f584bcd6f0892030386b6c2f9e` |

来源链路：`fchat_8.0.71.ipa`（砸壳）→ `ipa_rebrand.py` 改 id/改名 → 替换官方图标 → `WeChat_clean.ipa`。

`ipa_rebrand.py` 依赖：Python 3.9+、macOS 自带 `unzip` / `zip` / `codesign`，无第三方库。
`dl2.py` 依赖：仅 Python 标准库。

---

## 七、产物校验示例

```
bundle id           com.tencent.xin1
CFBundleDisplayName WeChat
CFBundleShortVersionString 8.0.71 / 8.0.71.34
UISupportedDevices  已删除
主可执行权限        -rwxr-xr-x (0755)
签名                not signed at all（交由 Sideloadly / SideStore 重签）
ad-hoc 重签验证     codesign --verify --deep --strict → PASS
```

---

## 八、一图流

```
砸壳 IPA ──► ipa_rebrand.py ──► WeChat.ipa (com.tencent.xin1)
                                      │
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
            Sideloadly 首次安装              SideStore（iloader 部署）
              （3 个 App ID 内）              ＋ pairing file ＋ LocalDevVPN
                     │                                 │
                     └────────────┬────────────────────┘
                                  ▼
                      7 天后：手机上点 "7 DAYS" 续签
```
