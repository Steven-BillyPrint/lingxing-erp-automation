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
  健康检查。
- 本机正式客户端更新必须由用户打开现有已安装客户端，并通过程序内置更新器完成。除非用户在
  当前任务中另行明确授权，否则 Codex 不得直接替换正式安装目录、客户端 EXE 或桌面快捷方式。
- 源码仓库不是客户端安装目录。仓库中的 `dist/`、`release-staging/` 和其他构建输出均为
  可重新生成的未跟踪产物，不得作为正式客户端入口，也不得由安装器或更新器自动回写。
- `CLIENT_VERSION` 是源码中的客户端版本权威；发布包内的 `VERSION.txt` 只描述该发布包，
  不代表源码仓库必须保存对应 EXE。不得通过单独修改版本文件绕过程序内置版本及内容校验。
- `.codex-tmp/`、`output/`、`tmp/`、运行数据库、日志和用户文件不是源码，不得加入提交。
