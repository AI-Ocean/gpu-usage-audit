.PHONY: build run clean

BINARY  := dist/v2
# VERSION 은 환경 변수로 덮어쓸 수 있다: `VERSION=v0.1 make build`.
VERSION ?= dev
# INTERVAL 도 환경 변수로 덮어쓸 수 있다: `INTERVAL=200ms make run`.
# 짧은 데모용으로 200ms~1s 가 학습 시연에 잘 맞는다.
INTERVAL ?= 30s

build:
	mkdir -p dist
	go build -ldflags="-X main.version=$(VERSION)" -o $(BINARY) .

# run 은 데몬을 무한 루프로 시작한다. Ctrl+C 로 종료.
run: build
	./$(BINARY) --db /tmp/v2.db --interval $(INTERVAL)

clean:
	rm -rf dist /tmp/v2.db
