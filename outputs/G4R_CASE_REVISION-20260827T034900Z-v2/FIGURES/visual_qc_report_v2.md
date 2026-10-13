# visual_qc_report_v2

本报告对PNG像素、300 DPI、SVG结构、关键关系方向和文字安全条件做确定性检查。

- figure_1_case_process.svg: PASS; pixels=2501x1099; dpi=(299.9994, 299.9994); {'nonblank': True, 'size': True, 'dpi': True, 'svg': True}
- figure_2_perspective_hypothesis_matrix.svg: PASS; pixels=2087x1678; dpi=(299.9994, 299.9994); {'nonblank': True, 'size': True, 'dpi': True, 'svg': True}
- figure_3_evidence_viewpoint_dependency.svg: PASS; pixels=2501x1215; dpi=(299.9994, 299.9994); {'nonblank': True, 'size': True, 'dpi': True, 'svg': True}
- figure_4_disagreement_to_monitoring.svg: PASS; pixels=2617x1353; dpi=(299.9994, 299.9994); {'nonblank': True, 'size': True, 'dpi': True, 'svg': True}
- figure_5_viewpoint_governance.svg: PASS; pixels=2547x1353; dpi=(299.9994, 299.9994); {'nonblank': True, 'size': True, 'dpi': True, 'svg': True}

- 图3关系方向：PASS；94条字段级依赖标注在Evidence→Viewpoint边，不绘制为业务节点。
- 图4分支文字：PASS；使用中文描述性标签，不使用纯ID作为主要标签。
- 图5修订差异：PASS；两个修订前后表述不同，且修订后明确保留不确定性。
- 图2图例：PASS；包含“支持”和“条件支持”，并在caption中声明格数不是投票。
- 中文字体/白底/黑灰+低饱和蓝/黑白可辨识：PASS。
- contact_sheet.png：PASS。

总体验收：PASS。
