// RUN: mlir-opt --test-data-layout-query --split-input-file --verify-diagnostics %s | FileCheck %s

module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 32, abi = 32, preferred = 64>>,
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space<5>>,#ptr.spec<size = 64, abi = 64, preferred = 64>>,
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space<4>>, #ptr.spec<size = 32, abi = 64, preferred = 64, index = 24>>,
  #dlti.dl_entry<"dlti.default_memory_space", #test.const_memory_space<7>>,
  #dlti.dl_entry<"dlti.alloca_memory_space", #test.const_memory_space<5>>,
  #dlti.dl_entry<"dlti.global_memory_space", #test.const_memory_space<2>>,
  #dlti.dl_entry<"dlti.program_memory_space", #test.const_memory_space<3>>,
  #dlti.dl_entry<"dlti.stack_alignment", 128 : i64>
>} {
  // CHECK-LABEL: @spec
  func.func @spec() {
    // CHECK: alignment = 4
    // CHECK: alloca_memory_space = #test.const_memory_space<5>
    // CHECK: bitsize = 32
    // CHECK: default_memory_space = #test.const_memory_space<7>
    // CHECK: global_memory_space = #test.const_memory_space<2>
    // CHECK: index = 32
    // CHECK: preferred = 8
    // CHECK: program_memory_space = #test.const_memory_space<3>
    // CHECK: size = 4
    // CHECK: stack_alignment = 128
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space>
    // CHECK: alignment = 1
    // CHECK: alloca_memory_space = #test.const_memory_space<5>
    // CHECK: bitsize = 64
    // CHECK: default_memory_space = #test.const_memory_space<7>
    // CHECK: global_memory_space = #test.const_memory_space<2>
    // CHECK: index = 64
    // CHECK: preferred = 1
    // CHECK: program_memory_space = #test.const_memory_space<3>
    // CHECK: size = 8
    // CHECK: stack_alignment = 128
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space<3>>
    // CHECK: alignment = 8
    // CHECK: alloca_memory_space = #test.const_memory_space<5>
    // CHECK: bitsize = 64
    // CHECK: default_memory_space = #test.const_memory_space<7>
    // CHECK: global_memory_space = #test.const_memory_space<2>
    // CHECK: index = 64
    // CHECK: preferred = 8
    // CHECK: program_memory_space = #test.const_memory_space<3>
    // CHECK: size = 8
    // CHECK: stack_alignment = 128
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space<5>>
    // CHECK: alignment = 8
    // CHECK: alloca_memory_space = #test.const_memory_space<5>
    // CHECK: bitsize = 32
    // CHECK: default_memory_space = #test.const_memory_space<7>
    // CHECK: global_memory_space = #test.const_memory_space<2>
    // CHECK: index = 24
    // CHECK: preferred = 8
    // CHECK: program_memory_space = #test.const_memory_space<3>
    // CHECK: size = 4
    // CHECK: stack_alignment = 128
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space<4>>
    return
  }
}

// -----

module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 32, abi = 32, preferred = 32>>,
  #dlti.dl_entry<"dlti.default_memory_space", #test.const_memory_space>
>} {
  // CHECK-LABEL: @default_memory_space
  func.func @default_memory_space() {
    // CHECK: alignment = 4
    // CHECK: bitsize = 32
    // CHECK: index = 32
    // CHECK: preferred = 4
    // CHECK: size = 4
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space>
    // CHECK: alignment = 4
    // CHECK: bitsize = 32
    // CHECK: index = 32
    // CHECK: preferred = 4
    // CHECK: size = 4
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space<1>>
    // CHECK: alignment = 4
    // CHECK: bitsize = 32
    // CHECK: index = 32
    // CHECK: preferred = 4
    // CHECK: size = 4
    "test.data_layout_query"() : () -> !ptr.ptr<#test.const_memory_space<2>>
    return
  }
}

// -----

// expected-error@+2 {{preferred alignment is expected to be at least as large as ABI alignment}}
module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 64, abi = 64, preferred = 32>>
>} {
  func.func @pointer() {
    return
  }
}

// -----

// expected-error@+2 {{size entry must be divisible by 8}}
module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 33, abi = 32, preferred = 32>>
>} {
  func.func @pointer() {
    return
  }
}


// -----

// expected-error@+2 {{abi entry must be divisible by 8}}
module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 32, abi = 33, preferred = 64>>
>} {
  func.func @pointer() {
    return
  }
}


// -----

// expected-error@+2 {{preferred entry must be divisible by 8}}
module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 32, abi = 32, preferred = 33>>
>} {
  func.func @pointer() {
    return
  }
}


// -----

// expected-error@+2 {{index entry must be divisible by 8}}
module attributes { dlti.dl_spec = #dlti.dl_spec<
  #dlti.dl_entry<!ptr.ptr<#test.const_memory_space>, #ptr.spec<size = 32, abi = 32, preferred = 32, index = 33>>
>} {
  func.func @pointer() {
    return
  }
}
