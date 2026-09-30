# FedOps Helm

하나의 Chart에 환경별 설정 파일(`-f`) 또는 인자(`--set`)를 적용하여 FedOps를 설치하는 저장소임.

**카카오클라우드 기본 구성은 기존 설치 방식 그대로, 지정 VM 노드 + 로컬 디스크임. CSI 전환은 필수가 아님.** 클러스터에 노드가 여러 대 있어도 FedOps와 Task는 지정 노드에 배치함.

## 1. 설치 방식 선택

| 환경 | 프로파일 | FedOps 배치 | 저장소 |
|---|---|---|---|
| 온프레미스 싱글 노드 | `onpremise-single-node` | 지정 노드 | 로컬 디스크 |
| 온프레미스 멀티 노드 분산 | `onpremise-multi-node` | 스케줄러가 배치 | 기존 StorageClass |
| 카카오클라우드, 기존 방식 **기본** | `kakaocloud-local` | 지정 VM 노드 | 로컬 디스크, CSI 전환 없음 |
| 카카오클라우드 멀티 노드 분산 **선택** | `kakaocloud-multi-node` | 스케줄러가 배치 | 기존 StorageClass |

`deployment.topology`는 클러스터 노드 개수가 아니라 **FedOps 배치 방식**임. 따라서 카카오클라우드 노드가 4대여도 기존 방식은 `single-node`로 설정함. 온프레미스 멀티노드 클러스터에서도 동일하게 지정 노드 방식을 선택할 수 있음.

멀티노드 분산 모드는 앱을 여러 노드에 배치할 수 있도록 만든 옵션임. 모든 앱의 복제 수는 여전히 1이며, DB 이중화나 고가용성을 제공하는 것은 아님. StorageClass도 실제 노드·가용 영역에서 볼륨을 연결할 수 있어야 함. 노드별 로컬 볼륨을 만드는 StorageClass만 지정한다고 노드 간 데이터 이동이 가능해지는 것은 아님.

## 2. 준비 사항

- Kubernetes 허용 버전: `>=1.35.0-0 <1.37.0-0`. 그 외 버전은 이 Chart로 설치하지 않음.
- 로컬 도구: Helm, kubectl, Python 3 + PyYAML.
- 설치할 클러스터의 kubeconfig와 context를 명시적으로 지정함. 기존 운영 FedOps와 다른 대상인지 먼저 확인함.
- 한 클러스터에 FedOps 한 개 설치를 전제로 함. 일부 Service/RBAC 이름이 고정되어 있음.
- Istio와 ingress는 별도 설치임. 이 Chart가 클라우드 VM, 보안 그룹, 공인 IP 또는 StorageClass를 자동 생성하지 않음.
- 기본 외부 포트는 웹 `30080`, Task `30081~31080`(1,000개)임. 포트 수가 동시 학습 1,000개의 성능을 보장하지는 않음.
- 공개 이미지의 접근 가능 여부와 CPU 아키텍처를 설치 환경에서 확인함. 현재 이미지 조합은 amd64 환경을 대상으로 함.

```bash
git clone https://github.com/z8086486/FedOps-Helm.git
cd FedOps-Helm
export KUBECONFIG=/absolute/path/to/target-kubeconfig.yaml
export CTX=YOUR_TARGET_CONTEXT
export NS=fedops
kubectl --context="$CTX" cluster-info
kubectl --context="$CTX" get nodes -o wide
```

설정은 오른쪽 파일이 앞 파일을 덮어쓰며, `--set`은 파일보다 우선함. `sites/my-site.yaml`은 개인 환경 파일로 Git에서 제외함. 비밀번호는 values나 명령줄에 넣지 않음.

## 3. 환경별 설치 명령 (`-f`)

아래에서 **한 가지 방식만 선택**함. 실제 실행 전 4~6절의 저장소·Secret·Istio 준비를 먼저 완료함.

### 온프레미스 싱글 노드

```bash
cp sites/onpremise-single-node.example.yaml sites/my-site.yaml
# my-site.yaml의 nodeName, access.webHost, access.flHost, smtp.host를 수정함.
helm upgrade --install fedops ./charts/fedops \
  --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml \
  -f profiles/onpremise-single-node.yaml \
  -f sites/my-site.yaml
```

### 온프레미스 멀티 노드 분산

```bash
cp sites/onpremise-multi-node.example.yaml sites/my-site.yaml
# deployment.storageClass에 실제 동적 StorageClass 이름을 입력함.
# access.webHost, access.flHost, smtp.host도 수정함. nodeName은 지정하지 않음.
helm upgrade --install fedops ./charts/fedops \
  --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml \
  -f profiles/onpremise-multi-node.yaml \
  -f sites/my-site.yaml
```

### 카카오클라우드 — 현재 설치와 같은 방식

```bash
cp sites/kakaocloud-local.example.yaml sites/my-site.yaml
# nodeName: 실제 Kubernetes 노드 이름
# access.webHost / access.flHost: 접근 가능한 공인 IP 또는 DNS, 포트·경로 없이 입력함.
# smtp.host: 실제 사용 가능한 SMTP 서버
helm upgrade --install fedops ./charts/fedops \
  --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml \
  -f profiles/kakaocloud-local.yaml \
  -f sites/my-site.yaml
```

CSI 설치나 전환 없이 사용하는 기본 명령임. 로컬 데이터는 지정 노드에 남으므로 노드 삭제·교체 시 데이터 이전이 필요함.

### 카카오클라우드 — 분산 배치가 필요한 경우만 선택

```bash
cp sites/kakaocloud-multi-node.example.yaml sites/my-site.yaml
# 실제 사용 가능한 deployment.storageClass와 외부 주소·SMTP를 입력함.
helm upgrade --install fedops ./charts/fedops \
  --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml \
  -f profiles/kakaocloud-multi-node.yaml \
  -f sites/my-site.yaml
```

기존 로컬 디스크 설치에 이 설정을 바로 덮어쓰지 않음. PVC 변경 제약과 데이터 이전을 별도로 검토해야 함.

## 4. `--set`으로 선택하는 경우

3절 명령의 `-f profiles/환경.yaml` 대신 아래 인자를 사용하면 됨. 이미지 파일, `profiles/common.yaml`, 사이트 파일은 그대로 사용함.

| 환경 | 대체 인자 |
|---|---|
| 온프레미스 싱글 | `--set deployment.provider=onpremise --set deployment.topology=single-node --set deployment.storageMode=local` |
| 온프레미스 분산 | `--set deployment.provider=onpremise --set deployment.topology=multi-node --set deployment.storageMode=storage-class --set nodeName= --set deployment.storageClass=YOUR_STORAGE_CLASS` |
| 카카오클라우드 기본 | `--set deployment.provider=kakaocloud --set deployment.topology=single-node --set deployment.storageMode=local` |
| 카카오클라우드 분산 | `--set deployment.provider=kakaocloud --set deployment.topology=multi-node --set deployment.storageMode=storage-class --set nodeName= --set deployment.storageClass=YOUR_STORAGE_CLASS` |

예시 — 카카오클라우드 기본 방식:

```bash
helm upgrade --install fedops ./charts/fedops \
  --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml -f sites/my-site.yaml \
  --set deployment.provider=kakaocloud \
  --set deployment.topology=single-node \
  --set deployment.storageMode=local
```

## 5. 저장소와 인증 정보 준비

새 설치 전용 절차임. 기존 데이터가 있는 환경에는 초기화 스크립트를 실행하지 않음.

```bash
kubectl --context="$CTX" create namespace "$NS"
```

로컬 디스크 방식은 **선택한 VM/서버에 SSH 접속한 뒤**, 사이트 파일과 같은 폴더를 준비함. 아래 UID는 제공 이미지 조합 기준이며 이미지 변경 시 다시 확인함. Task는 현재 기본 런타임의 root 실행을 기준으로 함.

```bash
sudo install -d -m 0750 -o 999 -g 999 /data/fedops/fedops/mongo
sudo install -d -m 0750 -o 999 -g 999 /data/fedops/fedops/registry-mongo
sudo install -d -m 0750 -o 65532 -g 65532 /data/fedops/fedops/minio
sudo install -d -m 0755 -o root -g root /data/fedops/fedops/tasks
```

분산 방식에서는 이 로컬 폴더 준비 대신 `kubectl --context="$CTX" get storageclass`로 확인한 동적 StorageClass를 사용함. Task PVC는 Pod 생성 후 바인딩되도록 구현하여 `WaitForFirstConsumer` 방식도 처리함. CSI 드라이버와 볼륨 권한·용량·가용 영역은 해당 스토리지 운영 정책에 맞춰 준비함.

다시 로컬 터미널에서 실행함:

```bash
python3 scripts/bootstrap.py secrets \
  --kubeconfig "$KUBECONFIG" --context "$CTX" --namespace "$NS"
```

DB와 MinIO 인증 정보를 생성하여 Secret으로 직접 저장함. 파일이나 콘솔에 비밀번호를 출력하지 않음. 기존 관련 Secret/PVC가 있으면 중단함. 이 스크립트는 Chart 기본 Secret·버킷 이름만 지원함.

SMTP Secret `fedops-smtp`는 별도로 준비해야 함. 키는 `SPRING_MAIL_USERNAME`, `SPRING_MAIL_PASSWORD`임. Secret 관리 도구를 사용하거나, 접근을 제한한 로컬 파일 두 개에서 생성함:

```bash
kubectl --context="$CTX" -n "$NS" create secret generic fedops-smtp \
  --from-file=SPRING_MAIL_USERNAME=/secure/path/smtp-username \
  --from-file=SPRING_MAIL_PASSWORD=/secure/path/smtp-password
```

사이트 파일의 SMTP 주소·포트·TLS·인증 설정은 실제 메일 서버와 맞춰야 함. 메일 서버는 Chart에 포함되어 있지 않음.

## 6. Istio와 외부 포트 준비

기존에 호환되는 Istio가 설치되어 있으면 중복 설치하지 않음. 신규 전용 클러스터에서는 별도 설치함. 아래는 기존 배포에 사용한 버전이며 대상 Kubernetes와의 호환성을 확인 후 사용함.

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update istio
helm upgrade --install istio-base istio/base --version 1.30.5 \
  --kube-context "$CTX" -n istio-system --create-namespace --wait
helm upgrade --install istiod istio/istiod --version 1.30.5 \
  --kube-context "$CTX" -n istio-system --wait
```

FedOps 설치와 **동일한 values 파일**로 ingress 설정을 생성함. 아래 `PROFILE`은 선택한 환경에 맞게 변경함. `--set` 방식을 사용했다면 생성 명령에도 동일하게 전달함.

```bash
export PROFILE=kakaocloud-local
mkdir -p results
python3 scripts/render-ingress-values.py \
  --namespace "$NS" --service-type NodePort --web-port 30080 \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml -f "profiles/$PROFILE.yaml" \
  -f sites/my-site.yaml > results/ingress-values.yaml

helm upgrade --install fedops-ingress istio/gateway --version 1.30.5 \
  --kube-context "$CTX" -n istio-system \
  -f results/ingress-values.yaml --wait
```

Manager 할당 범위, Istio Gateway, ingress NodePort를 같은 1,000개 포트로 맞추는 구성임. 웹 포트를 바꾸면 `access.publicPort`와 생성기의 `--web-port`도 동일하게 변경함.

외부 네트워크에서는 웹 TCP 30080과 Task TCP 30081~31080을 필요한 접속자 범위에 허용함. 카카오클라우드는 공인 IP 연결과 보안 그룹, 온프레미스는 방화벽·필요 시 NAT 설정을 별도로 수행함. HTTP 예시는 테스트용이며 공용 운영 환경은 TLS 적용이 필요함. `LoadBalancer` 방식은 생성기의 `--service-type LoadBalancer`로 선택할 수 있지만 사업자별 포트 수 제한과 비용, 주소 설정을 별도로 확인해야 함.

이제 3절의 선택한 FedOps 설치 명령을 실행함. 초기 DB 앱 계정 생성 전에는 일부 앱이 Ready가 아닐 수 있어 최초 설치에 `--wait`를 넣지 않았음.

## 7. 초기화와 확인

```bash
kubectl --context="$CTX" -n "$NS" rollout status deployment/fedops-mongo-deploy --timeout=300s
kubectl --context="$CTX" -n "$NS" rollout status deployment/fedops1-registry-mongo --timeout=300s
kubectl --context="$CTX" -n "$NS" rollout status deployment/fedops1-registry-minio --timeout=300s
python3 scripts/bootstrap.py jobs \
  --kubeconfig "$KUBECONFIG" --context "$CTX" --namespace "$NS"
kubectl --context="$CTX" -n "$NS" wait --for=condition=complete \
  job/bootstrap-web-db job/bootstrap-registry-db job/bootstrap-object-storage --timeout=600s
kubectl --context="$CTX" -n "$NS" rollout restart \
  deployment/fedops-web-backend deployment/fedops1-registry deployment/fl-perf deployment/fl-server-st
kubectl --context="$CTX" -n "$NS" get pods,pvc,svc
```

브라우저에서 `http://외부주소:30080/fedops/`로 접속함. Agent Studio에도 같은 주소를 등록하고 HTTP 허용 옵션을 확인함.

설치 후 로그인 → Task·Registry 조회 → 파일 저장·다운로드 → Registry 등록 → 집계 서버 생성 → 클라이언트 1개 학습 → 글로벌 모델 다운로드 순서로 확인함. Task 생성 실패 시 Manager 로그, Task Pod 이벤트, PVC 상태, 외부 포트 접근을 확인함.

## 8. 변경 내용과 검증 범위

- 기존 `nodeName` 고정·로컬 PV 방식은 유지함.
- 환경과 배치·저장소 선택을 분리하고, 환경별 프로파일을 추가함.
- 분산 배치는 노드 고정을 해제하고 StorageClass PVC를 사용하도록 확장함. Manager의 Task 저장소 생성도 같은 선택을 따름.
- 공개 MinIO 이미지와 해당 이미지의 실행 권한 설정을 공통 프로파일에 반영함.
- Task 포트 최대 1,000개와 ingress 설정 생성기를 제공함.
- Task 삭제 시 다른 Task의 PV까지 포함할 수 있었던 조건을 해당 PVC와 정확히 일치하는 경우로 좁힘.

오프라인 검증:

```bash
python3 tests/verify_profiles.py
python3 tests/verify_helpers.py
helm lint charts/fedops --strict --kube-version 1.35.4 \
  -f charts/fedops/tests/static-values.yaml
```

프로파일 렌더링, 잘못된 설정 거부, Manager의 저장소 분기, ingress 포트 생성을 검사함. **멀티노드 분산 배치와 실제 연합학습 E2E는 이 버전에서 아직 실클러스터 검증하지 않았음.** 기존 카카오클라우드 단일 지정 노드 배포의 동작 경험과 신규 분산 모드 검증을 구분함. 이 저장소 작업 중 운영 클러스터에 적용하지 않았음.

## 구조

```text
charts/fedops/       공통 Helm Chart와 Manager 호환 코드
profiles/           환경·배치별 설정
sites/              사용자 환경 입력 예시
scripts/            초기 Secret/DB/버킷 준비, ingress 설정 생성
tests/              클러스터를 변경하지 않는 검사
```

설치 제거 시 PV/PVC가 보존될 수 있음. `helm uninstall`이 데이터까지 삭제한다는 의미는 아님. 기존 운영 배포에 적용하거나 저장소 방식을 바꿀 때는 데이터 이전·롤백 계획을 별도로 수립함.
