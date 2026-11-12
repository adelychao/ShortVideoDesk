# 平台图标

本目录是工作台共用的平台图标资源。前端通过 `src/platformLogos.ts` 按平台 ID 选择文件，并由 `PlatformLogo.vue` 统一处理展示及加载失败时的兜底。新增平台时应同时维护此目录与映射；微信视频号（`wechat_channels`）与微信公众号（`wechat`）使用不同图标。

以下素材来自对应平台站点，保留原始文件并在本地加载：

- `baijiahao.png`：[百家号创作者平台](https://baijiahao.baidu.com/)使用的应用图标（[原文件](https://pic.rmb.bdstatic.com/activity/2025-8/1754378389223/e8c461082fe3.png)）。
- `douyin.ico`：[抖音创作者服务中心](https://creator.douyin.com/)使用的站点图标（[原文件](https://lf3-static.bytednsdoc.com/obj/eden-cn/yvahlyj_upfbvk_zlp/ljhwZthlaukjlkulzlp/pc_creator/favicon_v2_7145ff0.ico)）。
- `toutiao.ico`：[今日头条](https://www.toutiao.com/)使用的站点图标（[原文件](https://sf3-cdn-tos.douyinstatic.com/obj/eden-cn/uhbfnupkbps/toutiao_favicon.ico)）。
- `wechat-channels.ico`：[视频号助手](https://channels.weixin.qq.com/)使用的站点图标（[原文件](https://res.wx.qq.com/t/wx_fed/finder/helper/finder-helper-web/res/favicon-v2.ico)）。

其余 SVG 沿用工作台已有素材。平台名称和标志归各自权利人所有。
