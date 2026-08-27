/* `vela` — run a compiled `.velac` image.
 *
 *     vela program.velac [args...]
 *
 * Arguments after the image are visible to the program through `arg_count`
 * and `arg_get`.
 */

#include <stdlib.h>
#include <string.h>

#include "vela.h"

static void usage(void)
{
    fprintf(stderr,
        "usage: vela <image.velac> [args...]\n"
        "\n"
        "Build an image with `velac build program.vela -o program.velac`.\n");
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        usage();
        return 64;
    }
    if (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0) {
        usage();
        return 0;
    }
    if (strcmp(argv[1], "--version") == 0) {
        printf("vela vm, image format %d\n", VELA_VERSION);
        return 0;
    }

    vm_init();

    char *error = NULL;
    Image *image = image_load(argv[1], &error);
    if (image == NULL) {
        fprintf(stderr, "vela: %s: %s\n", argv[1],
                error ? error : "could not load image");
        free(error);
        vm_free();
        return 65;
    }

    RunResult result = vm_run(image, argc - 2, argv + 2);

    /* Flush before tearing down, so output ordering is stable when stdout is
     * a pipe rather than a terminal. */
    fflush(stdout);
    fflush(stderr);

    image_free(image);
    vm_free();

    return result == RUN_OK ? 0 : 70;
}
