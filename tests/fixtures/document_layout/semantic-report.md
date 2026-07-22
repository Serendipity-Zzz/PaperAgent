# 驻波实验报告

## 实验目的

**目标**是验证弦上驻波关系 $f_n = nv / 2L$，并保留可追溯数据。

1. 配置采样参数
   - 采样率：5000 Hz
   - 基频：60 Hz
2. 运行实验

| 模态 | 频率 / Hz |
| --- | ---: |
| 1 | 60 |
| 2 | 120 |

```python
frequency = mode * 60
```

$$
y(x,t)=2A\sin(kx)\cos(\omega t)
$$

![驻波振幅分布](tiny-figure.svg)

结果与已有结论一致 [@fixture-source]。

## 参考文献

[@fixture-source]: Synthetic fixture for regression testing.
