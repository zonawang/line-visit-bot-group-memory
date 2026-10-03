# LINE Visit Bot Group Memory

讓同一隻企業參訪 LINE Bot 記住每個群組各自的活動資訊。私聊仍可詢問通用問題；群組中只有真正 `@Bot` 的文字訊息才會觸發回覆。

## 功能

- 使用 LINE `groupId` 或 `roomId` 區分不同參訪群組。
- 將活動名稱、日期、集合時間、集合地點、交通與聯絡人保存到 Firestore。
- 第一位輸入「建立參訪」的成員成為該群組的資料管理者。
- 只有資料管理者可以修改資訊，其他成員都能查詢。
- 資料尚未設定時明確告知，不自行猜測時間或地點。
- LINE Webhook 先驗證 `X-Line-Signature`，密鑰由 Secret Manager 提供給 Cloud Run。

## LINE 操作方式

先在群組使用 LINE 的提及功能選到 Bot：

```text
@Bot 建立參訪
@Bot 設定活動名稱 XX 大學企業參訪
@Bot 設定日期 2026/10/20
@Bot 設定集合時間 09:30
@Bot 設定集合地點 公司一樓
@Bot 設定交通 從捷運站步行五分鐘
@Bot 設定聯絡人 王小明 0912-345-678
```

群組成員可以查詢：

```text
@Bot 活動資訊
@Bot 幾點集合？
@Bot 集合地點在哪裡？
```

在群組輸入 `@Bot 管理說明` 可以再次查看設定指令。

## Firestore 資料

預設 collection 為 `visit_group_events`，也可用 `VISIT_GROUP_COLLECTION` 調整。每個 document ID 是 LINE 的 `groupId` 或 `roomId`：

```text
visit_group_events/{conversationId}
├── managerUserId
├── eventName
├── eventDate
├── meetingTime
├── meetingPlace
├── transportation
├── contact
├── createdAt
└── updatedAt
```

`managerUserId` 只用於修改權限判斷，不會顯示在 Bot 回覆中。

## 本機測試

測試使用記憶體版 store，不需要 Google Cloud 憑證：

```bash
python -m unittest discover -p 'test_*.py'
```

本機啟動服務需要 Application Default Credentials 可存取 Firestore，並設定：

```text
LINE_CHANNEL_SECRET
LINE_CHANNEL_ACCESS_TOKEN
VISIT_GROUP_COLLECTION（選填）
```

啟動：

```bash
pip install -r requirements.txt
python app.py
```

健康檢查為 `GET /ready`，LINE Webhook 路徑為 `POST /webhook`。

推送到 `main` 後，GitHub Actions 會建立容器並更新 `line-zona` 專案中的 `devrel-visit-bot` Cloud Run 服務。部署不會把 LINE token 或 Channel secret 寫入 GitHub；服務會繼續從 Secret Manager 讀取。

## 安全界線

- 不在程式碼或 GitHub 儲存 LINE token、Channel secret 或服務帳號金鑰。
- 群組必須先建立參訪資料，才能寫入場次資訊。
- 只有建立該場資料的 LINE 使用者能修改。
- 目前沒有提供聊天指令更換管理者，避免群組成員自行接管；需要更換時應由維運者確認後處理。
