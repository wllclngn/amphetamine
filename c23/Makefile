KDIR ?= /lib/modules/$(shell uname -r)/build
MODULE_NAME := amphetamine_kmod

# kbuild cannot handle spaces in M= path. Build in /tmp if CURDIR has spaces.
BUILD_DIR := $(shell echo '$(CURDIR)' | grep -q ' ' && echo /tmp/amphetamine-build || echo '$(CURDIR)')
NEED_COPY := $(shell echo '$(CURDIR)' | grep -q ' ' && echo 1 || echo 0)

# Auto-detect kernel compiler from .config (CachyOS uses clang/LLVM)
KERNEL_CC := $(shell grep -s '^CONFIG_CC_IS_CLANG=y' $(KDIR)/.config >/dev/null 2>&1 && echo clang || echo gcc)
ifeq ($(KERNEL_CC),clang)
  CC := clang
  LD := ld.lld
  LLVM_FLAG := LLVM=1
else
  CC := gcc
  LD := ld
  LLVM_FLAG :=
endif

SOURCES := $(wildcard *.c) $(wildcard *.h) Kbuild

all: _sync_build
	@echo "  Built $(MODULE_NAME).ko ($(KERNEL_CC) toolchain, build dir: $(BUILD_DIR))"

_sync_build:
ifeq ($(NEED_COPY),1)
	@mkdir -p $(BUILD_DIR)
	@cp -u $(SOURCES) $(BUILD_DIR)/ 2>/dev/null || cp $(SOURCES) $(BUILD_DIR)/
	$(MAKE) -C $(KDIR) M=$(BUILD_DIR) CC=$(CC) LD=$(LD) $(LLVM_FLAG) modules
	@cp '$(BUILD_DIR)/$(MODULE_NAME).ko' '$(CURDIR)/'
else
	$(MAKE) -C $(KDIR) M=$(BUILD_DIR) CC=$(CC) LD=$(LD) $(LLVM_FLAG) modules
endif

clean:
ifeq ($(NEED_COPY),1)
	@rm -rf $(BUILD_DIR)
	@rm -f $(MODULE_NAME).ko $(MODULE_NAME).o $(MODULE_NAME).mod* modules.order Module.symvers .*.cmd
else
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
endif

load: all
	@sudo rmmod $(MODULE_NAME) 2>/dev/null || true
	sudo insmod $(BUILD_DIR)/$(MODULE_NAME).ko debug=1
	@sleep 0.3
	@dmesg | grep amphetamine | tail -3

unload:
	sudo rmmod $(MODULE_NAME) 2>/dev/null || true
	@echo "  Module unloaded"

reload: unload load

install: all
	sudo cp $(BUILD_DIR)/$(MODULE_NAME).ko /lib/modules/$(shell uname -r)/extra/
	sudo depmod -a
	sudo modprobe $(MODULE_NAME)

uninstall:
	sudo rmmod $(MODULE_NAME) 2>/dev/null || true
	sudo rm -f /lib/modules/$(shell uname -r)/extra/$(MODULE_NAME).ko
	sudo depmod -a

status:
	@lsmod | grep $(MODULE_NAME) || echo "  Module not loaded"
	@ls -l /dev/amphetamine 2>/dev/null || echo "  /dev/amphetamine not found"
	@dmesg | grep amphetamine | tail -5

.PHONY: all _sync_build clean load unload reload install uninstall status
