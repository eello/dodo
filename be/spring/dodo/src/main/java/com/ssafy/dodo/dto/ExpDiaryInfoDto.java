package com.ssafy.dodo.dto;

import com.ssafy.dodo.entity.DiaryImage;
import com.ssafy.dodo.entity.ExpDiary;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;
import java.util.List;
import java.util.stream.Collectors;

@Getter
@NoArgsConstructor
public class ExpDiaryInfoDto {

    private String content;
    private Boolean isContainImage;
    private List<String> images;
    private LocalDateTime createdAt;
    private CategoryInfoDto category;

    public static ExpDiaryInfoDto of(ExpDiary expDiary) {
        List<String> images = expDiary.getImages().stream()
                .map(DiaryImage::getPath)
                .collect(Collectors.toList());

        CategoryInfoDto category = CategoryInfoDto.of(expDiary.getAddedBucket()
                .getPublicBucket()
                .getCategory());

        ExpDiaryInfoDto dto = new ExpDiaryInfoDto();
        dto.content = expDiary.getContent();
        dto.isContainImage = images.size() > 0;
        dto.images = images;
        dto.createdAt = expDiary.getCreatedAt();
        dto.category = category;
        return dto;
    }
}
