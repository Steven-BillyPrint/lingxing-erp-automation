# ERP 自动化仓库操作约束

## 正式发布与部署

- 只有用户在当前任务中明确审核通过并授权发布/部署后，才能执行正式操作。
- 所有源码先通过分支、测试和 PR 合并到 `main`；禁止从功能分支直接部署。
- Windows 客户端发布统一执行：

  ```powershell
  .\scripts\publish_client_release.ps1 -ConfirmProductionRelease
  ```

- 客户端 Release 发布成功后，服务器部署统一执行：

  ```powershell
  .\scripts\deploy_production.ps1 -ConfirmProductionDeployment
  ```

- 共享部署私钥固定保存在
  `Z:\同事个人\颜奕超\ERP自动化部署专用\codex-production-deploy-ed25519`，主机指纹保存在同目录的
  `known_hosts`。任务可以调用这些文件，但不得读取、打印、复制、提交或上传私钥内容。
- 该共享密钥在服务器端必须绑定强制命令，只能部署已合并的 `main`；不得把管理员 Shell
  私钥当作常规发布入口。
- 发布顺序固定为：完整 CI → 发布 GitHub Release → 确认无活动任务 → 部署服务器 →
  健康检查 → 更新本机正式客户端、项目 EXE 和桌面快捷方式。
- `.codex-tmp/`、`output/`、`tmp/`、运行数据库、日志和用户文件不是源码，不得加入提交。
