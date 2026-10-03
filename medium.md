# 讓每個參訪群組都有自己的記憶：我替 LINE Bot 加上 Firestore

[上一篇](https://medium.com/@zonawang/%E8%AE%93%E4%BC%81%E6%A5%AD%E5%8F%83%E8%A8%AA%E8%B3%87%E8%A8%8A%E6%9B%B4%E5%A5%BD%E6%89%BE-%E6%88%91%E5%81%9A%E4%BA%86%E4%B8%80%E9%9A%BB-line-bot-%E5%B0%8F%E5%B9%AB%E6%89%8B-06124af56590)，我做了一隻可以待在企業參訪群組裡的 LINE Bot。它能回答參訪前要準備什麼、流程大概怎麼進行，也知道在群組裡只有真的被 `@` 到時才要出聲。

不過，當時的它只能回答通用問題。如果有人問「幾點集合？」或「遊覽車可以停哪裡？」，Bot 並不知道大家正在討論哪一場參訪，也沒有地方可以保存每個群組自己的資料。

這次我替它加上了群組記憶。主辦人可以直接在 LINE 群組裡設定資訊，其他成員再用自己習慣的方式提問。不同學校、不同場次的資料會各自保存，不會互相混在一起。

## 同一隻 Bot，要記得不同群組的事

企業參訪同時進行時，通常每一場都有自己的工作群組。例如 A 學校的集合時間是早上九點，B 學校可能是下午兩點；兩個群組也會有不同的集合地點、聯絡人和注意事項。

LINE 傳送 Webhook 事件時，群組訊息裡會包含一個 `groupId`。這個值就像群組的識別碼，所以 Bot 收到訊息後，會先找出訊息來自哪一個群組：

```python
def conversation_id(source):
    if source.get("type") == "group":
        return source.get("groupId")
    if source.get("type") == "room":
        return source.get("roomId")
    return None
```

接著，我用 `groupId` 當成 Firestore 的 document ID。這樣同一隻 Bot 就能在同一個資料庫裡保存很多場參訪，而且查詢時只會讀取目前群組的內容。

```text
visit_group_events/{groupId}
├── managerUserId
├── eventName
├── eventDate
├── meetingTime
├── meetingPlace
├── transportation
├── contact
├── customFields
├── createdAt
└── updatedAt
```

## 先建立參訪，再從群組裡設定資料

第一次使用時，主辦人可以在群組輸入：

```text
@Bot 建立參訪
```

第一位建立參訪的人會成為這個群組的資料管理者。之後只有這位管理者可以修改資料，群組裡的其他成員都能查詢。

這個限制是為了避免任何人都能更改集合時間或地點。畢竟參訪資訊如果被改錯，真的可能讓大家跑錯地方。

主辦人接著可以設定常用資料：

```text
@Bot 設定活動名稱 XX 大學企業參訪
@Bot 設定日期 2026/10/20
@Bot 設定集合時間 09:30
@Bot 設定集合地點 公司一樓
@Bot 設定交通 從捷運站步行五分鐘
@Bot 設定聯絡人 Zona
```

Bot 收到設定指令後，會先確認這個 LINE 使用者是不是資料管理者，再把內容寫進 Firestore：

```python
if data.get("managerUserId") != manager_user_id:
    return "forbidden"

current_transaction.update(
    document,
    {
        field: value,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    },
)
```

這裡使用 Firestore transaction，讓「檢查管理者」和「更新內容」在同一次操作中完成。即使短時間內收到多個設定指令，也比較不容易發生資料互相覆蓋的問題。

## 參訪資訊很多，所以欄位不能先全部寫死

一開始，我只開放活動名稱、日期、集合時間、集合地點、交通和聯絡人。實際開始測試後，很快就遇到更多需求：

```text
@Bot 設定注意事項：參訪需全程攜帶訪客證
@Bot 設定報到方式：抵達一樓後至櫃台報到
@Bot 設定遊覽車停車地點：大港墘公園
```

如果每多一種資訊，我就要先修改一次程式，這隻 Bot 用起來還是不夠方便。因此我保留幾個常用欄位，再加上一個 `customFields`，讓主辦人可以自由決定欄位名稱。

```python
custom_fields = data.get("customFields") or {}
custom_fields[label] = value

current_transaction.update(
    document,
    {
        "customFields": custom_fields,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    },
)
```

設定完成後，輸入 `@Bot 活動資訊`，Bot 會把固定欄位和自訂欄位一起列出來。之後遇到新的參訪需求，也可以直接從 LINE 群組新增，不必先替 Bot 發布一個新版本。

## 資料存進去之後，還要聽得懂不同問法

測試「遊覽車停車地點」時，我遇到了一個很實際的問題。

我先設定：

```text
@Bot 設定遊覽車停車地點：大港墘公園
```

Bot 也正確回覆已經更新完成。但另一位成員接著問：

```text
@Bot 遊覽車可以停哪裡？
```

當時 Bot 沒有找到答案，反而回覆集合地點與交通資訊尚未設定。資料確實已經存在 Firestore，查詢規則卻只會尋找完整的「遊覽車停車地點」幾個字，所以沒有把兩種說法連在一起。

後來我把自訂欄位的查詢改成相似關鍵詞比對。程式會整理問題和欄位名稱，移除「可以」、「哪裡」、「請問」等問句常用詞，再比較兩邊共同出現的文字。

```python
question_text = match_text(question)
label_text = match_text(label)

common = longest_common_text(label_core, question_core)
shared_characters = len(set(label_core) & set(question_core))
coverage = shared_characters / len(set(label_core))

if len(common) >= 2 or (shared_characters >= 3 and coverage >= 0.5):
    # 把這個自訂欄位列為可能的答案
```

現在「遊覽車停車地點」可以對應到「遊覽車可以停哪裡」，「報到方式」也能對應到「要怎麼報到」。同時，我也加了避免誤判的條件；單純問「集合地點在哪裡」時，不會因為兩個欄位都有「地點」就回覆遊覽車停車位置。

文字規則可以處理「遊覽車停車地點」和「遊覽車可以停哪裡」這種有共同關鍵詞的問法，但如果設定的欄位叫「午餐」，有人問「中午吃什麼？」，兩邊只有一個字相同，規則就很難可靠判斷。

所以我再接上 Vertex AI Gemini。Bot 會先執行原本的文字規則；規則找不到答案時，才把使用者問題和這個群組現有的欄位名稱交給 Gemini，請它選出最相關的一個。

```python
selected_label = selector.select_label(
    question,
    list(saved_field_values),
)

if selected_label in saved_field_values:
    value = saved_field_values[selected_label]
```

這裡我沒有讓 Gemini 直接生成參訪答案。模型只能從 Firestore 已存在的欄位名稱中選一個，也可以回傳「沒有適合欄位」。程式收到結果後，還會再確認這個欄位真的存在，最後直接取出 Firestore 裡的原始內容。

例如群組已經保存：

```text
午餐：提供餐盒
```

有人問「中午吃什麼？」，Gemini 可以選擇「午餐」，Bot 再回覆資料庫裡的「提供餐盒」。Gemini 不會自己決定餐點，也不能臨時生成一個不存在的時間或地點。

如果 Vertex AI 暫時無法使用，程式會回到原本的規則；兩邊都找不到答案時，Bot 就有禮貌地請大家稍等 Zona 協助確認。這樣可以讓問法更有彈性，同時保留參訪資訊需要的可控性。

## 回答正確之外，說話方式也很重要

功能完成後，我又重新看了一次 Bot 的回答。原本的訊息雖然正確，但有些只有短短一句，看起來比較像系統通知。

現在設定成功時，它會回覆：

```text
好的，已經幫你更新完成：
遊覽車停車地點：大港墘公園
群組成員現在可以直接向我查詢這筆資訊。
```

查到資料時，它會先說明這是目前找到的內容，也會提醒大家如果現場安排有變動，要以最新公告為準。

如果問題真的超過目前保存的資料，Bot 也不會只丟回一段功能說明，而是有禮貌地回覆：

```text
不好意思，我目前還沒有足夠的資訊回答這個問題。
你可以先稍等 Zona 協助確認；也可以輸入「活動資訊」查看目前已設定的內容。
```

這樣學生知道問題已經被接住，也知道接下來可以等誰協助，不會因為 Bot 答不出來就卡在原地。

## 一則問題是怎麼找到群組資料的？

目前完整流程大概是這樣：

```text
LINE 群組訊息
    ↓
確認是否真的 @Bot
    ↓
用 groupId 讀取 Firestore
    ↓
比對固定欄位與自訂欄位
    ↓
組合成較完整、友善的回答
    ↓
透過 LINE Reply API 回到原群組
```

程式仍然跑在 Google Cloud Run。LINE 的 Channel secret 和 access token 放在 Secret Manager，沒有寫進 GitHub。每次推送到 `main`，GitHub Actions 會建立新的容器並自動部署到 Cloud Run。

這次也替 Cloud Run 的服務帳號加入 Firestore 讀寫與 Vertex AI 使用權限。Bot 使用自己的服務帳號存取這兩項服務，不需要在程式碼中放 Google Cloud 金鑰。

## 可以怎麼測試？

可以先建立一個 LINE 測試群組，把官方帳號 `@920ksdcl` 邀請進去，再使用 LINE 的提及功能選到 Bot：

```text
@Bot 建立參訪
@Bot 設定活動名稱 測試企業參訪
@Bot 設定注意事項：請攜帶訪客證
@Bot 設定遊覽車停車地點：大港墘公園
```

接著換另一位群組成員提問：

```text
@Bot 活動資訊
@Bot 有什麼要注意的？
@Bot 遊覽車可以停哪裡？
```

也可以故意問一個還沒設定的問題，看看它會不會誠實說目前沒有資料，並請大家稍等 Zona 協助確認。

## 從常見問答，變成每個群組自己的參訪助手

加入 Firestore 之後，這隻 Bot 已經可以記得每個群組自己的活動名稱、日期、地點和各種臨時新增的資訊。主辦人只要在 LINE 裡設定一次，後續加入群組的成員也能自己查詢。

這次實作也讓我更清楚看到，一隻工作群組 Bot 除了需要保存資料，還要處理權限、不同問法和回覆語氣。這些小地方會直接影響大家願不願意真的在群組裡使用它。

接下來，我想繼續觀察企業參訪現場還有哪些重複問題，再讓這個群組助手慢慢變得更實用。

## 本篇完整程式碼

[GitHub 專案](https://github.com/zonawang/line-visit-bot-group-memory)
