# オンボーディング

オープンクリニックAI の iOS アプリ（Capacitor）です。Xcode のターゲット名は `App`、バンドル ID は `com.ditensive.openclinic` です。

## クローン直後に起動できなかった理由

Debug 構成が、リポジトリに含まれていない設定ファイルを参照していました。

`App.xcodeproj/project.pbxproj` の `debug.xcconfig` は、プロジェクトの1つ上のフォルダを指していました。

```text
path = ../debug.xcconfig
sourceTree = SOURCE_ROOT
```

このリポジトリの場所が `OpenclinicIOS` だと、Xcode は次のパスを開こうとします。

```text
/Users/yuji/Desktop/Detensive/debug.xcconfig
```

そのファイルはクローンに含まれないため、シミュレータ起動時に次のエラーになります。

```text
Unable to open base configuration reference file '.../debug.xcconfig'.
```

Debug のプロジェクト設定と App ターゲットの両方が、このファイルを `baseConfigurationReference` にしているため、ファイルが無いと Debug ビルドを開始できません。

## 入れた対応

1. リポジトリ直下に `debug.xcconfig` を追加した。
2. `project.pbxproj` の参照を、プロジェクト直下の `debug.xcconfig` に変更した。

```text
path = debug.xcconfig
sourceTree = SOURCE_ROOT
```

`debug.xcconfig` の内容は次のとおりです。

- `CAPACITOR_DEBUG = true`  
  `App/Info.plist` の `CAPACITOR_DEBUG` が `$(CAPACITOR_DEBUG)` を読むため、デバッグ起動時にこの値を渡します。
- `#include? "Pods/Target Support Files/Pods-App/Pods-App.debug.xcconfig"`  
  CocoaPods を使う場合だけ、その設定を読みます。`?` 付きなので、Pods が無くてもエラーになりません。このプロジェクトの依存関係は Swift Package（`CapApp-SPM`）です。

## 起動手順

1. Xcode で `App.xcodeproj` を開く。すでに開いている場合は一度閉じて開き直す。参照パスを変えたあとは、開いたままのウィンドウに古いエラーが残ることがあります。
2. 初回は Swift Package の解決が走り、`capacitor-swift-pm` 8.5.3 を取得します。ネットワークが必要です。
3. 実行先に iPhone シミュレータを選び、Run（`⌘R`）する。

```bash
open App.xcodeproj
```
