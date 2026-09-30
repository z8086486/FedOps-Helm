# FedOps Helm

기존 F서버 Helm과 카카오클라우드에서 검증한 **로컬 디스크·PV/PVC 방식**을 기반으로 함. 하나의 Chart에서 `-f` 또는 `--set`으로 설치 환경과 배치 방식을 선택함.

## 배치 방식

| 환경 | 프로파일 | 앱 배치 | DB·MinIO·학습 Task |
|---|---|---|---|
| 온프레미스 싱글노드 | `onpremise-single-node` | 지정 노드 | 같은 노드의 로컬 디스크 |
| 온프레미스 멀티노드 | `onpremise-multi-node` | 여러 노드에 배치 가능 | 지정 저장 노드에 고정 |
| 카카오클라우드 기존 방식 | `kakaocloud-local` | 지정 VM 노드 | 같은 노드의 로컬 디스크 |
| 카카오클라우드 멀티노드 | `kakaocloud-multi-node` | 여러 VM 노드에 배치 가능 | 지정 저장 노드에 고정 |

`nodeName`은 싱글노드에서는 전체 앱의 노드, 멀티노드에서는 **DB·MinIO·Task의 저장 노드**임. 웹·Backend·Registry API·Manager·Performance·메일 Gateway는 멀티노드에서 다른 노드에 배치할 수 있음. 별도 저장장치 드라이버나 NAS를 설치하지 않음.

멀티노드는 저장소 복제나 자동 장애 복구를 의미하지 않음. 앱은 각각 1개 Pod이며, 저장 노드가 멈추면 데이터 서비스와 학습도 영향을 받음. `ScheduleAnyway` 분산 설정은 선호 조건이므로 모든 노드에 반드시 배치되는 것은 아님. 확실한 역할 분리는 아래 `workloads.*.nodeSelector`로 지정함.

## 1. 대상과 사이트 설정

필수 도구는 Helm, kubectl, Python 3 + PyYAML임. 허용 Kubernetes 버전은 `>=1.35.0-0 <1.37.0-0`임. 현재 이미지 조합은 amd64 환경을 기준으로 함. 한 클러스터에 FedOps 한 개 설치를 전제로 하며 기존 운영 클러스터에 무심코 적용하지 않음.

```bash
git clone https://github.com/z8086486/FedOps-Helm.git
cd FedOps-Helm
export KUBECONFIG=/absolute/path/to/target-kubeconfig.yaml
export CTX=YOUR_TARGET_CONTEXT
export NS=fedops
kubectl --context="$CTX" get nodes -o wide
```

### 카카오클라우드 멀티노드

```bash
cp sites/kakaocloud-multi-node.example.yaml sites/my-site.yaml
# nodeName: DB·MinIO·Task 데이터를 둘 실제 Kubernetes 노드 이름
# access.webHost / access.flHost: 외부 IP 또는 DNS, 포트·경로 없이 입력
# smtp.host: 사용 가능한 SMTP 서버

helm upgrade --install fedops ./charts/fedops \
  --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml \
  -f profiles/kakaocloud-multi-node.yaml \
  -f sites/my-site.yaml
```

위 명령은 **2~4절의 최초 설치 준비를 완료한 뒤** 실행함. 기본 저장 용량은 사이트 예시에서 조정함. 예시에 경로가 없으면 `values.yaml`의 `/data/fedops/fedops/{mongo,registry-mongo,minio,tasks}`를 사용함.

### 다른 환경의 설치 명령

환경마다 해당 예시를 새로 복사하고 값을 입력함. 서로 다른 환경 설정을 이어서 덮어쓰지 않음.

```bash
# 온프레미스 싱글노드
cp sites/onpremise-single-node.example.yaml sites/my-site.yaml
# 사이트 값 입력 후:
helm upgrade --install fedops ./charts/fedops --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml -f profiles/common.yaml \
  -f profiles/onpremise-single-node.yaml -f sites/my-site.yaml

# 온프레미스 멀티노드
cp sites/onpremise-multi-node.example.yaml sites/my-site.yaml
# 사이트 값 입력 후:
helm upgrade --install fedops ./charts/fedops --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml -f profiles/common.yaml \
  -f profiles/onpremise-multi-node.yaml -f sites/my-site.yaml

# 카카오클라우드 기존 단일 지정 노드 방식
cp sites/kakaocloud-local.example.yaml sites/my-site.yaml
# 사이트 값 입력 후:
helm upgrade --install fedops ./charts/fedops --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml -f profiles/common.yaml \
  -f profiles/kakaocloud-local.yaml -f sites/my-site.yaml
```

### `--set`으로 같은 설정 선택

환경 프로파일 대신 `deployment.provider`와 `deployment.topology`를 지정함.

```bash
helm upgrade --install fedops ./charts/fedops --kube-context "$CTX" -n "$NS" \
  -f charts/fedops/examples/images-minsoojo.yaml \
  -f profiles/common.yaml -f sites/my-site.yaml \
  --set deployment.provider=kakaocloud \
  --set deployment.topology=multi-node
```

온프레미스는 `provider=onpremise`, 싱글노드는 `topology=single-node`로 변경함. 뒤의 파일과 `--set`이 앞의 값을 덮어씀.

앱별 노드를 정하려면 사이트 파일에 다음처럼 추가함. 실제 노드 이름으로 변경해야 함. DB·MinIO·Task는 이 설정과 관계없이 `nodeName`에 고정됨.

```yaml
workloads:
  frontend:
    nodeSelector: {kubernetes.io/hostname: APP_NODE_A}
  backend:
    nodeSelector: {kubernetes.io/hostname: APP_NODE_B}
  manager:
    nodeSelector: {kubernetes.io/hostname: APP_NODE_C}
```

## 2. 저장 폴더 준비

**nodeName에 지정한 저장 서버에 SSH 접속하여** 사이트 설정과 같은 경로를 준비함. 다음 소유권은 제공한 이미지 조합 기준임. Task 기본 런타임은 root 실행을 기준으로 함.

```bash
sudo install -d -m 0750 -o 999 -g 999 /data/fedops/fedops/mongo
sudo install -d -m 0750 -o 999 -g 999 /data/fedops/fedops/registry-mongo
sudo install -d -m 0750 -o 65532 -g 65532 /data/fedops/fedops/minio
sudo install -d -m 0755 -o root -g root /data/fedops/fedops/tasks
```

Helm이 기본 DB·MinIO의 정적 local PV/PVC를 생성함. Task는 Manager가 해당 저장 노드의 hostPath PV/PVC를 생성함. `sc-<Task>` 이름이 표시되어도 외부 디스크가 자동 생성되는 방식은 아님.

## 3. Secret과 메일 준비

이 절차는 **빈 데이터로 시작하는 신규 설치 전용**임. 기존 데이터가 있다면 계정 초기화를 실행하지 않음.

```bash
kubectl --context="$CTX" create namespace "$NS"
python3 scripts/bootstrap.py secrets \
  --kubeconfig "$KUBECONFIG" --context "$CTX" --namespace "$NS"
```

DB·MinIO용 새 인증 정보를 Secret으로 직접 저장함. 값을 파일이나 콘솔에 출력하지 않으며 기존 관련 Secret/PVC가 있으면 중단함. 스크립트는 기본 Secret·버킷 이름만 지원함.

SMTP는 별도로 준비함. 기본 Secret 이름은 `fedops-smtp`, 키는 `SPRING_MAIL_USERNAME`, `SPRING_MAIL_PASSWORD`임. 예를 들어 접근을 제한한 로컬 파일에서 생성함:

```bash
kubectl --context="$CTX" -n "$NS" create secret generic fedops-smtp \
  --from-file=SPRING_MAIL_USERNAME=/secure/path/smtp-username \
  --from-file=SPRING_MAIL_PASSWORD=/secure/path/smtp-password
```

SMTP 주소·포트·TLS·인증 설정은 실제 메일 서버와 맞춤. 실습용 Mailpit을 사용한다면 별도로 설치하고 SMTP 인증·TLS를 끈 설정을 사용함. 메일 서버는 Chart에 포함되지 않음. 인증 정보는 values/Git에 넣지 않음.

## 4. Istio와 외부 연결 준비

호환되는 Istio가 이미 있으면 중복 설치하지 않음. 신규 전용 클러스터는 다음처럼 준비함. 예시는 검증에 사용한 버전임.

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update istio
helm upgrade --install istio-base istio/base --version 1.30.5 \
  --kube-context "$CTX" -n istio-system --create-namespace --wait
helm upgrade --install istiod istio/istiod --version 1.30.5 \
  --kube-context "$CTX" -n istio-system --wait

export PROFILE=kakaocloud-multi-node
mkdir -p results
python3 scripts/render-ingress-values.py --namespace "$NS" \
  --service-type NodePort --web-port 30080 \
  -f charts/fedops/examples/images-minsoojo.yaml -f profiles/common.yaml \
  -f "profiles/$PROFILE.yaml" -f sites/my-site.yaml > results/ingress-values.yaml
helm upgrade --install fedops-ingress istio/gateway --version 1.30.5 \
  --kube-context "$CTX" -n istio-system -f results/ingress-values.yaml --wait
```

FedOps와 ingress 생성기에 동일한 파일·인자를 전달함. 웹은 `30080`, Task는 `30081~31080`(1,000개)임. 웹 포트를 바꾸면 `access.publicPort`와 `--web-port`를 함께 바꿈. ingress는 `externalTrafficPolicy=Cluster`로 다른 노드의 Pod에도 전달함.

공인 IP, 방화벽·보안 그룹, 필요한 NAT는 별도 준비함. 노드 간 Pod·Service 통신도 가능해야 함. 외부 접속 허용 범위는 필요한 대상으로 제한함. HTTP 예시는 실습용이며 공개 운영에는 TLS 설정이 필요함. 1,000개 포트가 동시 학습 1,000개의 처리 성능을 보장하지는 않음.

이제 1절에서 선택한 FedOps 설치 명령을 실행함. 첫 DB 초기화 전에 앱이 Ready가 아닐 수 있어 최초 설치 명령에 `--wait`는 넣지 않았음.

## 5. 초기화와 확인

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
kubectl --context="$CTX" -n "$NS" get pods -o wide
kubectl --context="$CTX" -n "$NS" get pvc
```

`http://외부주소:30080/fedops/`로 접속함. Agent Studio에도 같은 주소를 등록하고 HTTP 사용을 허용함. 로그인 → Registry 등록 → 집계 서버 생성 → 1개 클라이언트 학습 → 모델 저장 순서로 확인함.

## 검증과 주의 사항

2026-09-30 카카오클라우드 Kubernetes 1.35.4에서 0.3.0 멀티노드 프로파일로 기존 FedOps를 삭제 후 새로 설치하여 확인함.

- 저장 노드 1대와 앱 노드 3대, 총 4대에 실제 분리 배치함.
- 웹·인증 메일·가입·로그인·파일 저장/다운로드·Agent Studio 연결 통과함.
- 새 MNIST Task의 로컬 학습·게시 전 검사·Registry 게시·집계 서버 생성 통과함.
- FedAvg 1 client·1 round 완료 및 글로벌 모델 파일 저장·읽기·SHA-256 확인함.
- 1,000개 Task 포트 설정을 확인하고, 학습 포트와 추가 표본 3개 포트의 외부 연결을 확인함.

공개 MNIST 2,048개 표본을 사용한 API 기반 통합 테스트임. Server Validation OFF이며 전체 UI·1,000개 동시 Task·온프레미스 실배포까지 검증했다는 의미는 아님. 클라이언트 참여와 모델 파일 저장을 확인했으며 Registry의 신규 학습 결과 ModelVersion 자동 게시까지 확인한 것은 아님.

```bash
python3 tests/verify_profiles.py
python3 tests/verify_helpers.py
helm lint charts/fedops --strict --kube-version 1.35.4 \
  -f charts/fedops/tests/static-values.yaml
```

검사 전용 `static-values.yaml`은 배포하지 않음. `sites/my-site.yaml`, kubeconfig, 인증 파일은 Git에 올리지 않음.

0.3.0은 로컬 저장소 기반으로 멀티노드를 통일한 버전임. 0.2.0의 `deployment.storageMode`, `deployment.storageClass`, `task.storageClass`, `task.nodeSelector`, `persistence.*.storageClass` 옵션은 제거함. 오래된 설정을 그대로 사용하면 검증에서 거부함. 저장 노드를 바꾸는 것은 데이터 이전을 의미하므로 단순 인자 변경으로 처리하지 않음.

DB·MinIO는 단일 인스턴스이며 저장 노드 장애 복구·다중 클라이언트 부하·고가용성은 별도 검증 대상임. Helm uninstall 시 데이터가 보존될 수 있으므로 완전 삭제와 재설치는 별도 절차임.
