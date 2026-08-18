# VNav 数据预览

本地查看 `tmp_data/` 中的多视角视频、占据图、位姿、速度和最近 30 秒轨迹。

## 启动

```bash
npm install
npm run dev
```

然后访问 <http://127.0.0.1:5173>。页面会自动扫描以下成对目录：

```text
tmp_data/meta_<数据集ID>/
tmp_data/videos_<数据集ID>/
```

## 验证

```bash
npm test
npm run build
```

生产方式运行时，先执行 `npm run build`，再执行 `npm start`。
