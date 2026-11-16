# 桌面工具微信服务号扫码登录接口

桌面端使用 RuoYi-Lxz 服务号网页 OAuth 接口。RuoYi `AjaxResult` 中通过 `put` 加入的
字段位于响应顶层，不包在 `data` 对象内；客户端也兼容 `/getInfo` 的顶层 `user` 对象。

服务地址可通过 `WECHAT_AUTO_API_BASE_URL` 覆盖，默认 `https://discount.yidaw.cn/prod-api`。
认证请求使用 `Authorization: Bearer <token>`。

## 获取授权 URL

`GET /wxMpAuthUrl`，无需登录。后端返回：

```json
{
  "code": 200,
  "url": "https://open.weixin.qq.com/connect/oauth2/authorize?...&state=...#wechat_redirect",
  "state": "random-state"
}
```

桌面端在本地将 `url` 渲染为二维码，不把授权 URL 上传给第三方二维码服务。

## OAuth 回调与桌面轮询

微信服务号 OAuth 回调页将获得的 `code`、`state` 提交到 `POST /wxMpLogin`：

```json
{"code":"oauth-code","state":"random-state"}
```

回调接口在微信用户点击“使用完整服务”并取得本次 OAuth 的昵称、头像后，才按现有账号关联逻辑登录或创建用户，并返回顶层 `token` 和 `user`。仅扫码而未完成个人信息授权时，桌面端继续等待，不得视为登录成功。授权状态有效期为 300 秒。

桌面端调用新增的 `GET /wxMpLoginStatus?state=random-state`，无需登录。响应状态为
`WAITING`、`CONFIRMED`、`FAILED` 或 `EXPIRED`。成功时返回：

```json
{"code":200,"status":"CONFIRMED","fullServiceAuthorized":true,"token":"ruoyi-token","user":{"nickName":"微信昵称","avatar":"/profile/avatar.png"}}
```

微信 OAuth 令牌的 `is_snapshotuser=1` 表示首次扫码进入快照页。此时后端返回独立的引导页面并将状态置为 `WAITING_FULL_SERVICE`；用户点击“使用完整服务”并完成跳转后，后端处理新的 OAuth 回调。桌面端只接受同时包含 `status=CONFIRMED`、`fullServiceAuthorized=true`、`token`、`user.nickName` 和 `user.avatar` 的轮询结果，不使用已有后台资料补成本次微信授权信息。旧版后台未返回 `fullServiceAuthorized` 时，桌面端保持等待并提示更新后台。

## 获取用户与退出

- `GET /getInfo`：Bearer token；当前 RuoYi 控制器返回顶层 `user`，桌面端读取 `user.nickName`、`user.avatar`。
- `GET /wechat/helper/userInfo`：Bearer token；按当前后台登录用户读取或初始化额度资料，不再接收微信 `wxid`。如已部署的数据库表包含 `wh_wechat_info.wxid`，先执行后台仓库 `sql/20261115_drop_wh_wechat_info_wxid.sql`。
- `GET /wechat/helper/clientConfig`：Bearer token；返回桌面版本、更新下载地址、客服微信号及客服二维码文本。相关参数在 RuoYi “系统管理 → 参数设置”中维护，首次部署可导入 `sql/20261115_wechat_helper_client_settings.sql`。二维码内容由桌面端本地编码生成。
- 参数键：`wechat.helper.desktop.version`、`wechat.helper.desktop.downloadUrl`、`wechat.helper.customer.wxid`、`wechat.helper.customer.qrCode`。发布新版时将后台版本改为与桌面 `DESKTOP_VERSION` 相同的版本号，并配置下载链接；客服二维码参数填写要编码的文本内容。
- `POST /logout`：RuoYi Security 已有退出接口，带 Bearer token。

桌面端每次点击“开始”都会先请求 `/getInfo` 验证登录态。新增 `/wxMpLoginStatus` 必须允许匿名访问，因为桌面端在取到 token 之前尚未登录。
