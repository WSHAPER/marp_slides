"""marp_slides: a Nextcloud ExApp that turns a prompt into a PPTX slide deck.

An inner ``core:text2text`` TaskProcessing task (scheduled via OCS on whatever
text provider the instance has configured) authors Marp Markdown from the user
prompt; a native, editable PPTX is built from it with python-pptx (marp-cli
serves as the browserless syntax gate). The validated Markdown source is
retained alongside the PPTX, and deck images arrive as task file inputs.
"""
