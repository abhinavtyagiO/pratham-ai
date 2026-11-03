# Product context and slice boundary

Source: [shared product discussion](https://chatgpt.com/share/6ae9e340-9088-83ee-87db-faf1ddb52e4f)
and [the attached MVP build brief](PRODUCT_BRIEF.md). Both were read before implementation.

The product protects an Indian SMB owner's attention while representing the owner's operating
rules. Business memory, caller identity, owner policy, and uncertainty handling form the longer-term
product. The first operating environment is an owner-managed MRF/tyre retail store with Hindi and
Hinglish callers. Voice cloning is not central to the product.

Calls will eventually route through cloud telephony. The future Android-first owner app is a
control panel; the architecture does not depend on a handset app intercepting cellular audio.
Speech/telephony vendor choice remains open for a measured comparison of candidate stacks.

The agreed engineering sequence is a thin, reviewable vertical slice with tests, traces, and logs
before expanding the product. The attached brief narrows the first slice to a deterministic
simulation of one price enquiry. That scope takes precedence over the larger MVP feature list
discussed earlier in the conversation.

The first owner policy prohibits price disclosure and invites the caller to the shop, which
closes at 18:30 Asia/Kolkata. The model receives only permitted facts after policy evaluation.
The transcript, summary, applied rule, result, and trace make the behavior inspectable.

Uncertainty is recorded for the owner after the call; there is no live transfer. Availability and
claim policies were discussed for the broader MVP, but are not implemented in slice 1. Calls about
those subjects are currently `UNKNOWN` and need owner attention.

The delivered slice is ready for code/product review. Later slices require an explicit next task.
