# Ảnh sản phẩm và avatar trên Object Storage S3 của CMC Cloud

Ảnh không lưu vào PostgreSQL (DBaaS) và không đi qua pod. Database chỉ giữ URL công khai của ảnh.

```
Trình duyệt ──1. POST /api/sellers/me/uploads | /api/auth/me/avatar/upload {contentType, size}──▶ live-manager | auth-service
            ◀─────────────── presigned PUT URL (hết hạn 300 s, đã ký content-type + content-length) ──
            ──2. PUT file ──────────────────────────────────────────────────────────▶ CMC S3 (bucket)
            ──3. lưu publicUrl: POST /api/sellers/me/products {imageUrl}
                                PUT  /api/sellers/me/avatar {avatarUrl}
                                PUT  /api/auth/me/avatar {avatarUrl} ─────────────▶ DB chỉ lưu URL
```

| Loại ảnh | Ai upload | Thư mục trong bucket (server quyết định) | Lưu URL ở |
|---|---|---|---|
| Ảnh sản phẩm | Người bán (token `lcs_…`) | `products/<sellerId>/<uuid>.<ext>` | `products.image_url` (product-service) |
| Avatar người bán | Người bán | `avatars/sellers/<sellerId>/<uuid>.<ext>` | `sellers.avatar_url` (live-manager) |
| Avatar người mua | Người mua (JWT) | `avatars/users/<userId>/<uuid>.<ext>` | `users.avatar_url` (auth-service) |

Các ràng buộc phía server:
- Chỉ nhận JPEG, PNG hoặc WebP, tối đa 5 MB.
- S3 trả 403 nếu file thực tế khác loại hoặc khác kích thước so với lúc ký.
- Khi gán avatar, URL phải là ảnh do hệ thống cấp, nằm đúng thư mục của chính người đó; URL tùy ý bị trả 400.
- Khi đổi avatar, ảnh cũ bị xóa khỏi bucket.
- Nếu chưa cấu hình S3, các API upload trả 503. Mọi chức năng khác vẫn chạy bình thường.

## 1. Tạo bucket và access key trên CMC Cloud Portal

1. Trên Portal, vào **S3 Storage** và tạo bucket, ví dụ `live-commerce-media-dev`.
2. Tạo access key và secret key. Không gửi hai giá trị này qua chat, không commit vào repo.
3. Ghi lại **endpoint** của region chứa bucket, lấy từ Portal.
   - Theo tài liệu CMC, endpoint có dạng `https://s3.<region>.cloud.cmctelecom.vn`, ví dụ Hà Nội là `https://s3.hn-1.cloud.cmctelecom.vn`.
   - Endpoint của HCM cần xem trên Portal.
   - Hệ thống dùng kiểu path-style: `<endpoint>/<bucket>/<key>`.

Endpoint phải dùng HTTPS. Trang web chạy HTTPS, nên nếu S3 dùng HTTP thì trình duyệt sẽ chặn vì mixed content.

## 2. Thêm vào file secrets rồi deploy

File secrets nằm ngoài repo, mặc định là `~/lc-secrets/dev.env`. Nếu file đã có từ trước, thêm các dòng sau vào cuối:

```bash
S3_ENDPOINT=https://s3.<region>.cloud.cmctelecom.vn
S3_REGION=us-east-1
S3_BUCKET=live-commerce-media-dev
S3_PUBLIC_URL=
S3_FORCE_PATH_STYLE=true
S3_ACCESS_KEY_ID=<access key>
S3_SECRET_ACCESS_KEY=<secret key>
```

`S3_PUBLIC_URL` để trống thì hệ thống dùng `<S3_ENDPOINT>/<S3_BUCKET>`. Chỉ cần điền nếu bạn đặt CDN hoặc domain riêng trước bucket.

`scripts/deploy.sh` sẽ tạo Secret `object-storage` trong `live-commerce-dev`. live-manager và auth-service đọc Secret này qua `envFrom` với `optional: true`. Sau khi đổi Secret, cần restart pod:

```bash
ENV=dev SECRETS_ONLY=1 bash scripts/deploy.sh
kubectl -n live-commerce-dev rollout restart deploy/live-manager deploy/auth-service
```

## 3. Cấu hình bucket: đọc công khai ảnh và CORS

Bucket mặc định là riêng tư. Script `live-commerce-app/scripts/s3-setup.cjs` làm ba việc, và chạy lại bao nhiêu lần cũng được:
- Tạo bucket nếu chưa có.
- Gắn policy cho phép mọi người `GetObject` trên `products/*` và `avatars/*`. Phần còn lại của bucket vẫn riêng tư.
- Gắn CORS cho phép PUT và GET từ domain của web.

Script chạy trong image live-manager, vì image này đã có AWS SDK. Lệnh dưới đây chạy từ gốc repo app:

```bash
IMG=192.168.6.194/live-commerce/live-manager:<tag có S3> && kubectl -n live-commerce-dev run s3-setup --rm -i --quiet --restart=Never --image=$IMG --overrides="{\"spec\":{\"imagePullSecrets\":[{\"name\":\"harbor-registry\"}],\"containers\":[{\"name\":\"s3-setup\",\"image\":\"$IMG\",\"stdin\":true,\"stdinOnce\":true,\"workingDir\":\"/app\",\"command\":[\"node\",\"-\"],\"env\":[{\"name\":\"S3_CORS_ORIGINS\",\"value\":\"https://cmc-cloud-hcm1.cloud\"}],\"envFrom\":[{\"secretRef\":{\"name\":\"object-storage\"}}]}]}}" < scripts/s3-setup.cjs
```

Kết quả mong đợi:

```
bucket live-commerce-media-dev: đã có
policy: đọc công khai products/*, avatars/*
cors: PUT/GET từ https://cmc-cloud-hcm1.cloud
```

CMC S3 chạy trên Ceph RGW, có hỗ trợ bucket policy và CORS. Nếu Portal không cho key quyền `PutBucketPolicy`/`PutBucketCors`, hãy cấu hình hai mục này trực tiếp trên Portal với nội dung tương đương.

## 4. Kiểm tra

1. Mở Creator Studio, bấm vào avatar để đổi ảnh. Sau đó vào "Thêm sản phẩm" và bấm khung ảnh để chọn file.
2. Đăng nhập người mua, vào trang **Đơn hàng của tôi** và bấm vào avatar.
3. Mở DevTools, tab Network. Request PUT phải đi thẳng tới `s3.…cloud.cmctelecom.vn` và trả 200. Không có request nào chứa ảnh gửi tới `/api`.
4. Kiểm tra trong DB:
   ```sql
   SELECT avatar_url FROM sellers WHERE avatar_url IS NOT NULL;
   ```
   Kết quả chỉ là URL.

| Triệu chứng | Nguyên nhân |
|---|---|
| Upload trả 503 "Chưa cấu hình lưu trữ ảnh" | Chưa có Secret `object-storage`, hoặc chưa restart pod |
| Console báo CORS ở bước PUT | Chưa chạy bước 3, hoặc `S3_CORS_ORIGINS` khác domain đang mở |
| PUT trả 403 `SignatureDoesNotMatch` | Sai key, sai `S3_REGION`, đồng hồ node lệch, hoặc file khác type/size đã khai |
| Ảnh upload xong nhưng hiển thị lỗi 403 | Thiếu policy đọc công khai (bước 3) |
| Ảnh cũ không bị xóa khi đổi avatar | Pod không ra được Internet tới endpoint S3. Việc này không ảnh hưởng người dùng; xem log `Không xóa được` |

Việc ký URL làm hoàn toàn trong pod, không gọi mạng. Pod chỉ gọi S3 khi xóa ảnh cũ.

## Local (docker-compose)

docker-compose có sẵn service `s3` chạy RustFS, tương thích S3: có kiểm tra chữ ký, policy và CORS. Service `s3-init` chạy cùng script cấu hình như trên. Console RustFS ở `http://localhost:9001`.

Container gọi `s3:9000`, còn trình duyệt dùng `localhost:9000`. Vì vậy compose đặt `S3_PRESIGN_ENDPOINT` và `S3_PUBLIC_URL` trỏ về `localhost`. Trên K8s với CMC S3 không cần `S3_PRESIGN_ENDPOINT`.
