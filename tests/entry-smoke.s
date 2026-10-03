.text
.globl _start
_start:
    bl _main
    mov x16, #1
    svc #0x80
