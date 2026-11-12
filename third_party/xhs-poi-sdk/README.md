# 小红书地点搜索运行依赖

地点搜索通过固定版本的 [catbus-cli](https://github.com/cv-cat/catbus) 使用小红书创作者中心接口，Node.js 需满足该包声明的版本要求。首次部署或重装依赖时，在工作台项目根目录执行：

```powershell
npm ci --prefix third_party/xhs-poi-sdk --ignore-scripts
```

工作台按所选账号从 SAU 读取现有登录状态，搜索时在临时目录创建隔离会话，并在请求结束后清理。地点搜索接口不是小红书公开的稳定接口；账号失效或平台变更时会返回错误。当前上游实现使用固定坐标参与地点排序，搜索时可输入城市和地点名称来缩小范围。
