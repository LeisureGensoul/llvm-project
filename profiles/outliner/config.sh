# 编译参数：各版本、NoLTO/FullLTO 共用
COMPILE_FLAGS='-Oz -DNDEBUG -mno-red-zone'
LLVM_OPTIONS=(
  -enable-machine-outliner=always
  -outliner-leaf-descendants=true
  -machine-outliner-reruns=0
)
# 正确性：末项提交；测试选择见 tests.list
TEST_TARGETS='AArch64;X86'
CHECK_BUILD_TARGETS=(SupportTests llvm-config llvm-readobj llc FileCheck llvm-objdump opt llvm-cgdata
                     llvm-lto2 llvm-profdata split-file not count)
