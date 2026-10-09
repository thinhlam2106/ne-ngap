# Né Ngập – bản đồ ngập TP.HCM

Bản đồ ngập TP.HCM theo mưa, triều, người đi đường báo và **AI xem camera giao thông**.

- `index.html`: toàn bộ trang bản đồ, một file duy nhất.
- `scanner/scan.py`: bộ quét. AI xem ảnh camera giao thông TP.HCM và nhận ra chỗ đang ngập.
- `.github/workflows/scan.yml`: lịch chạy bộ quét 10 phút một lần trên GitHub Actions, miễn phí với kho công khai.
- `data/flood-ai.json`: kết quả AI. Bản đồ đọc file này 2 phút một lần.

## Cách hoạt động

1. 10 phút một lần, GitHub Actions chạy `scanner/scan.py`.
2. Bộ quét xem lượng mưa và mực triều (Open-Meteo):
   - **Đang mưa hoặc triều cao:** xem **tất cả** khoảng 795 camera mỗi lần chạy.
   - **Trong 2 giờ sau khi tạnh** (nước còn đọng): xem tất cả camera, 20 phút một lần.
   - **Trời khô:** chỉ xem khoảng 170 camera gần các điểm hay ngập, 3 giờ một lần.
3. Ảnh mỗi camera được thu nhỏ rồi gửi cho Claude Haiku 5.5, mỗi lần 8 ảnh. AI trả về mức nước theo bánh xe máy: không ngập, mắt cá, nửa bánh, ngang bô, lút yên.
4. Để tránh báo nhầm, một chỗ chỉ được đưa lên bản đồ khi:
   - AI rất chắc (độ tin cậy từ 0,85), **hoặc**
   - AI thấy ngập ở 2 lần quét liên tiếp.

   Chỗ ngập tự gỡ khi lần quét sau thấy hết ngập, hoặc khi quá 35 phút không có lượt quét mới.
5. Kết quả ghi vào `data/flood-ai.json` và tự đẩy lên kho. Bản đồ hiện mục "AI xem camera" và các chấm camera có viền đen. Chỉ đường tự né những chỗ này.

## Cài đặt (khoảng 15 phút)

### 1. Tạo kho trên GitHub

1. Đăng nhập github.com, bấm **New repository**.
2. Đặt tên `ne-ngap` và chọn **Public**. Kho công khai được chạy GitHub Actions miễn phí không giới hạn phút.
3. Trong kho mới, bấm **Add file → Upload files**. Kéo vào **toàn bộ nội dung** thư mục này, gồm `index.html`, `README.md`, `.nojekyll`, thư mục `data`, `scanner` và `.github`, rồi bấm **Commit changes**.

   Trên macOS, thư mục `.github` bị ẩn. Bấm `Cmd + Shift + .` trong Finder để hiện nó. Nếu kéo thả vẫn thiếu thư mục `.github`, làm cách sau:
   - Bấm **Add file → Create new file**.
   - Gõ tên `.github/workflows/scan.yml`.
   - Dán nội dung file `scan.yml` vào rồi commit.

### 2. Lấy khoá API của Claude

1. Vào console.anthropic.com, đăng ký và đăng nhập.
2. Vào **Plans & Billing**, nạp tiền bằng thẻ Visa/Mastercard.
3. Nên đặt **giới hạn chi tiêu** (spend limit) hằng tháng để an toàn.
4. Vào **API Keys → Create Key**, chép khoá. Khoá bắt đầu bằng `sk-ant-`.

### 3. Gắn khoá vào kho

1. Trong kho GitHub, vào **Settings → Secrets and variables → Actions → New repository secret**.
2. Đặt Name là `ANTHROPIC_API_KEY`, Secret là khoá vừa chép.

Khoá cất trong "Secrets" không ai xem được, kể cả khi kho công khai.

Có thể chỉnh thêm ở thẻ **Variables** (không bắt buộc):

- `DAILY_BUDGET_USD`: trần chi phí mỗi ngày, mặc định `1.5` USD. Chạm trần thì bộ quét tự dừng tới hôm sau.
- `DRY_SCAN_HOURS`: lúc trời khô thì bao nhiêu giờ xem camera một lần, mặc định `3`.

### 4. Bật trang web

1. Vào **Settings → Pages**.
2. Ở **Source**, chọn **Deploy from a branch**, Branch `main`, thư mục `/ (root)`, rồi **Save**.
3. Sau 1–2 phút, bản đồ có tại `https://<tên-tài-khoản>.github.io/ne-ngap/`.

### 5. Chạy thử bộ quét

1. Vào thẻ **Actions**. Nếu GitHub hỏi, bấm bật workflow.
2. Chọn **AI xem camera ngập → Run workflow**, đánh dấu **Quét tất cả camera ngay**, rồi bấm **Run workflow**.
3. Mở lần chạy đó, xem bước "AI xem camera". Lần chạy tốt có các dòng như:
   ```
   Tải được 7xx ảnh, lỗi ...
   Xong: AI xem 7xx ảnh, ... chỗ ngập đã xác nhận ...
   ```
4. Mở bản đồ, thẻ "Đang ngập" sẽ hiện "AI xem ... ảnh camera lúc ...". Kết quả mới có thể trễ tới 5 phút do GitHub lưu đệm.

Từ đó bộ quét tự chạy 10 phút một lần. GitHub có thể chạy trễ vài phút vào giờ đông.

## Chi phí ước tính

| Tình huống | Chi phí |
|---|---|
| Một lượt xem cả 795 camera | khoảng 0,04 USD |
| Một ngày có cơn mưa 1,5 giờ (9 lượt lúc mưa + 6 lượt sau mưa) | khoảng 0,65 USD |
| Một ngày mưa 3 giờ (18 + 6 lượt) | khoảng 1 USD |
| Ngày khô (8 lượt × 170 camera) | khoảng 0,07 USD |

Một tháng mùa mưa (khoảng 20 ngày mưa) tốn chừng 15 USD. Trần mặc định 1,5 USD/ngày giữ tối đa khoảng 45 USD/tháng. Muốn giữ chi phí thấp hơn nữa thì hạ trần; ngày mưa rất dài thì bộ quét sẽ dừng sớm hơn. Chi phí thật của mỗi lượt được ghi trong `data/flood-ai.json` (mục `stats.cost_usd`, `spend_today_usd`).

## Giới hạn cần biết

- **AI có thể nhầm.** Đường ướt bóng ban đêm dễ bị tưởng là ngập. Ống kính dính mưa thì ảnh nhoè. Vì vậy:
  - Chỗ ngập chỉ hiện khi AI chắc hoặc thấy ở 2 lượt liên tiếp.
  - Bản đồ luôn kèm ảnh camera mới nhất để người xem tự kiểm tra.
  - Người xem có nút sửa mức nước nếu AI sai.
- **Kết quả bị trễ:** 10 phút giữa hai lượt, cộng tới 5 phút lưu đệm của GitHub.
- **Phụ thuộc hệ thống camera.** Ảnh lấy từ hệ thống camera giao thông công khai của TP.HCM (Notis vận hành, cũng là nguồn của giaothong.hochiminhcity.gov.vn). Camera hỏng hoặc mất tín hiệu sẽ bị bỏ qua. Nếu máy chủ GitHub bị chặn tải ảnh, log sẽ báo "lỗi" cho hầu hết camera.
- **Dùng dữ liệu công khai.** Bộ quét tải mỗi camera một ảnh mỗi 10 phút, tức khoảng 1–2 ảnh mỗi giây trong lúc quét. Đây là mức nhẹ. Nếu đưa vào sử dụng thật lâu dài, nên báo và xin phép Trung tâm Quản lý điều hành giao thông TP.HCM.

## Chạy thử trên máy (không cần khoá, dùng dữ liệu giả lập)

```
pip install -r scanner/requirements.txt
MOCK=1 SCAN_ALL=true python scanner/scan.py
```

Nguồn dữ liệu: camera giao thông TP.HCM (Notis, giaothong.hochiminhcity.gov.vn); lượng mưa và triều (Open-Meteo); bản đồ © OpenStreetMap; điểm có nguy cơ ngập (Phòng CSGT Công an TP.HCM, 10/2026).
