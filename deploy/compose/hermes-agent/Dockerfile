ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a
FROM ${HERMES_IMAGE}

COPY --chmod=0755 entrypoint.sh /usr/local/bin/dgx-hermes-entrypoint

ENTRYPOINT ["/usr/local/bin/dgx-hermes-entrypoint"]
CMD ["gateway", "run"]
